"""Pipeline-owned types and protocols for Bio-Formats ingestion.

The `ResolutionInfo` record carries `index`, `size_x`, `size_y`, `size_z`, `size_c` and its
remaining declared fields.

The backend uses the selected Java/Bio-Formats installation and image-series identity. Image
access is exposed through pipeline-owned records so callers retain the reviewed metadata and
regional coordinate frame."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from numbers import Integral
from pathlib import Path
import stat
from typing import Protocol, runtime_checkable

import numpy as np
from ome_types import OME

from .errors import (
    InvalidPlaneError,
    InvalidRegionError,
    InvalidResolutionError,
    InvalidSeriesError,
)

# ==============================================================================
#                                      Base
# ==============================================================================


def _is_integer_scalar(value: object) -> bool:
    return isinstance(value, Integral) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class OpenedSourceIdentity:
    """IO-owned identity snapshot for the exact file opened by a reader."""

    resolved_path: Path
    file_sha256: str
    size_bytes: int
    modified_time_ns: int
    device_id: int
    inode_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.resolved_path, Path) or not self.resolved_path.is_absolute():
            raise ValueError("resolved_path must be an absolute Path")
        if (
            not isinstance(self.file_sha256, str)
            or len(self.file_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.file_sha256)
        ):
            raise ValueError("file_sha256 must be a lowercase SHA-256")
        for field_name in ("size_bytes", "modified_time_ns", "device_id", "inode_id"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a nonnegative integer")


def image_identity_member(path: str | Path) -> Path:
    """Locate the regular member that carries an image's filesystem identity.

    A file-backed image uses its own file. A processed-image directory uses its
    root ``zarr.json`` record, which identifies the completed generation in our
    image readers. We keep the directory as the image locator; only its retained
    filesystem identity and metadata checksum belong to this member.

    This function checks the locator and member types without reading metadata
    or pixels. The image reader checks the completed generation and its format.
    Missing paths, symbolic links, reparse points and nonregular members cannot
    supply an image identity and raise OSError or ValueError.

    Args:
        path: Image file or processed-image directory, retaining the caller's
            path spelling and resolution policy.

    Returns:
        The regular image file or its root metadata member.
    """

    source = Path(path)
    status = source.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(status.st_mode) or getattr(status, "st_file_attributes", 0) & reparse_flag:
        raise ValueError(f"image identity locator is a link or reparse point: {source}")
    if stat.S_ISDIR(status.st_mode):
        member = source / "zarr.json"
        status = member.lstat()
    else:
        member = source
    if not stat.S_ISREG(status.st_mode) or getattr(status, "st_file_attributes", 0) & reparse_flag:
        raise ValueError(f"image identity member is not a regular file: {member}")
    return member


def snapshot_opened_source_identity(
    path: Path, *, retained: OpenedSourceIdentity | None = None,
) -> OpenedSourceIdentity:
    """Hash a newly inspected source, or verify its recorded filesystem identity.

    We calculate a content digest when the source is first inspected. Later
    opens and sampling checks reuse that digest when the resolved path, size,
    modification time, device and inode still match. This avoids reading an
    entire slide again to open or sample a small region. A changed file raises
    ValueError so its image and acquisition metadata can be inspected again.
    """

    resolved = Path(path).resolve(strict=True)
    record = image_identity_member(resolved)
    before = record.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"source is not a regular file: {resolved}")
    before_identity = (
        before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino,
    )
    if retained is not None:
        if not isinstance(retained, OpenedSourceIdentity):
            raise TypeError("retained must be an OpenedSourceIdentity")
        expected = (
            retained.size_bytes, retained.modified_time_ns,
            retained.device_id, retained.inode_id,
        )
        if resolved != retained.resolved_path or before_identity != expected:
            raise ValueError(f"Source image changed after inspection; inspect it again: {resolved}")
        return retained
    digest = hashlib.sha256()
    with record.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = record.stat()
    after_identity = (
        after.st_size,
        after.st_mtime_ns,
        after.st_dev,
        after.st_ino,
    )
    if before_identity != after_identity:
        raise ValueError(f"source changed while its identity was captured: {resolved}")
    return OpenedSourceIdentity(
        resolved_path=resolved,
        file_sha256=digest.hexdigest(),
        size_bytes=after.st_size,
        modified_time_ns=after.st_mtime_ns,
        device_id=after.st_dev,
        inode_id=after.st_ino,
    )


@dataclass(frozen=True, slots=True)
class PlaneIndex:
    t: int = 0
    c: int = 0
    z: int = 0

    def __post_init__(self) -> None:
        if not all(_is_integer_scalar(value) for value in (self.t, self.c, self.z)):
            raise InvalidPlaneError(
                "Plane indices must be integer scalars and not booleans: " f"{self}"
            )
        for field_name in ("t", "c", "z"):
            object.__setattr__(self, field_name, int(getattr(self, field_name)))
        if min(self.t, self.c, self.z) < 0:
            raise InvalidPlaneError(f"Plane indices must be non-negative: {self}")


@dataclass(frozen=True, slots=True)
class Region:
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if not all(
            _is_integer_scalar(value) for value in (self.x, self.y, self.width, self.height)
        ):
            raise InvalidRegionError(
                "Region coordinates and extents must be integer scalars and not "
                f"booleans: {self}"
            )
        for field_name in ("x", "y", "width", "height"):
            object.__setattr__(self, field_name, int(getattr(self, field_name)))
        if self.x < 0 or self.y < 0 or self.width <= 0 or self.height <= 0:
            raise InvalidRegionError(f"Invalid half-open region: {self}")


@dataclass(frozen=True, slots=True)
class ResolutionInfo:
    index: int
    size_x: int
    size_y: int
    size_z: int
    size_c: int
    size_t: int
    rgb_samples: int
    dtype: np.dtype
    dimension_order: str
    is_thumbnail: bool

    def __post_init__(self) -> None:
        """Validate the ResolutionInfo fields when the record is constructed.

        The checks cover ``index``, ``size_x``, ``size_y``, ``size_z``, ``size_c``,
        ``size_t``. Invalid field types, values or combinations raise the declared
        validation error before this instance can be used by its caller."""

        numeric_values = (
            self.index,
            self.size_x,
            self.size_y,
            self.size_z,
            self.size_c,
            self.size_t,
            self.rgb_samples,
        )
        if not all(_is_integer_scalar(value) for value in numeric_values):
            raise InvalidResolutionError(
                "Resolution index, dimensions, and RGB sample count must be integer "
                f"scalars and not booleans: {self}"
            )
        for field_name in (
            "index",
            "size_x",
            "size_y",
            "size_z",
            "size_c",
            "size_t",
            "rgb_samples",
        ):
            object.__setattr__(self, field_name, int(getattr(self, field_name)))
        if self.index < 0:
            raise InvalidResolutionError(f"Resolution index must be non-negative: {self.index}")
        if min(self.size_x, self.size_y, self.size_z, self.size_c, self.size_t) <= 0:
            raise InvalidResolutionError(f"Resolution dimensions must be positive: {self}")
        if self.rgb_samples <= 0:
            raise InvalidResolutionError(f"RGB sample count must be positive: {self.rgb_samples}")
        try:
            normalized = np.dtype(self.dtype)
        except (TypeError, ValueError) as exc:
            raise InvalidResolutionError(f"Invalid dtype: {self.dtype!r}") from exc
        object.__setattr__(self, "dtype", normalized)


@dataclass(frozen=True, slots=True)
class SourceStorageBits:
    """Selected-series storage bits independently read from an explicit TIFF tag."""

    bits: int
    origin: str

    def __post_init__(self) -> None:
        if not _is_integer_scalar(self.bits) or not 0 < int(self.bits) <= 64:
            raise InvalidSeriesError("source storage bits must be a positive integer <= 64")
        if self.origin != "tiff_bits_per_sample":
            raise InvalidSeriesError("source storage bits require a recognized origin")
        object.__setattr__(self, "bits", int(self.bits))


@dataclass(frozen=True, slots=True)
class SeriesInfo:
    index: int
    name: str
    resolutions: tuple[ResolutionInfo, ...]
    storage_bits: SourceStorageBits | None = None

    def __post_init__(self) -> None:
        if not _is_integer_scalar(self.index):
            raise InvalidSeriesError(
                "Series index must be an integer scalar and not a boolean: " f"{self.index!r}"
            )
        object.__setattr__(self, "index", int(self.index))
        if self.index < 0:
            raise InvalidSeriesError(f"Series index must be non-negative: {self.index}")
        object.__setattr__(self, "resolutions", tuple(self.resolutions))
        if not self.resolutions:
            raise InvalidSeriesError("A series must contain at least one resolution")
        if tuple(level.index for level in self.resolutions) != tuple(range(len(self.resolutions))):
            raise InvalidSeriesError("Resolution indices must be consecutive from zero")
        if self.storage_bits is not None and not isinstance(self.storage_bits, SourceStorageBits):
            raise InvalidSeriesError("series storage evidence must be IO-owned")


@dataclass(frozen=True, slots=True)
class BackendVersions:
    backend: str
    bffile_version: str
    bioformats_maven_coordinate: str
    bioformats_runtime_version: str


@runtime_checkable
class BioFormatsSource(Protocol):
    @property
    def identity(self) -> OpenedSourceIdentity: ...

    @property
    def metadata(self) -> OME: ...

    @property
    def series(self) -> tuple[SeriesInfo, ...]: ...

    @property
    def versions(self) -> BackendVersions: ...

    def read_region(
        self,
        *,
        series: int,
        resolution: int,
        plane: PlaneIndex,
        region: Region,
    ) -> np.ndarray: ...

    def close(self) -> None: ...

    def __enter__(self) -> BioFormatsSource: ...

    def __exit__(self, exc_type, exc_value, traceback) -> None: ...


@runtime_checkable
class BioFormatsBackend(Protocol):
    def open(self, path: Path) -> BioFormatsSource: ...
