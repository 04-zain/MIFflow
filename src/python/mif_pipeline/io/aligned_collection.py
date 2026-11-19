"""Persist and stream one already registered image backed by original files.

Only metadata is assembled. Pixel regions use each member's existing reader,
with a bounded reader cache and no intermediate merged image.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import os
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cycle_registration import AcceptedCycleRegistration

import numpy as np

from .backends import (BackendVersions, InvalidPlaneError, InvalidRegionError,
    InvalidResolutionError, InvalidSeriesError, PlaneIndex, Region, SeriesInfo,
    SourceClosedError, OpenedSourceIdentity, snapshot_opened_source_identity)

SCHEMA_NAME = "mif-aligned-source-collection"
SUFFIX = ".mif-source.json"
MAX_DESCRIPTOR_BYTES = 1024 * 1024
MAX_MEMBERS = 256
MAX_CHANNELS = 4096
COLLECTION_NAMESPACE = "urn:mif-pipeline:aligned-source-collection:v1"


def is_aligned_source_collection(path: str | Path) -> bool:
    return str(path).lower().endswith(SUFFIX)


def _index(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a zero-based nonnegative integer")
    return value


def _closed(value, fields, label):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError(f"Invalid {label} fields")
    return value


@dataclass(frozen=True, slots=True)
class AlignedSourceMember:
    path: Path
    image_index: int
    series_index: int
    file_sha256: str
    channel_count: int
    identity: OpenedSourceIdentity

    def __post_init__(self):
        path = Path(self.path)
        if not path.is_absolute() or ".." in path.parts or is_aligned_source_collection(path):
            raise ValueError("Collection members need absolute, non-nested image paths")
        object.__setattr__(self, "path", path)
        _index(self.image_index, "image_index")
        _index(self.series_index, "series_index")
        if not 1 <= _index(self.channel_count, "channel_count") <= MAX_CHANNELS:
            raise ValueError("Invalid member channel count")
        if (type(self.file_sha256) is not str or len(self.file_sha256) != 64
                or any(c not in "0123456789abcdef" for c in self.file_sha256)):
            raise ValueError("Collection members require their inspected SHA-256")
        if (not isinstance(self.identity, OpenedSourceIdentity)
                or self.identity.resolved_path != path or self.identity.file_sha256 != self.file_sha256):
            raise ValueError("Collection member identity must match its inspected path and digest")

    def to_mapping(self):
        from .canonical_ingestion import _source_identity_mapping
        return dict(path=str(self.path), image_index=self.image_index,
            series_index=self.series_index, file_sha256=self.file_sha256,
            channel_count=self.channel_count, identity=_source_identity_mapping(self.identity))


@dataclass(frozen=True, slots=True)
class AlignedSourceChannel:
    member_index: int
    channel_index: int
    imaging_cycle: int

    def __post_init__(self):
        for field in ("member_index", "channel_index", "imaging_cycle"):
            _index(getattr(self, field), field)

    def to_mapping(self):
        return dict(member_index=self.member_index, channel_index=self.channel_index,
                    imaging_cycle=self.imaging_cycle)


@dataclass(frozen=True, slots=True)
class AlignedSourceCollection:
    name: str
    members: tuple[AlignedSourceMember, ...]
    channels: tuple[AlignedSourceChannel, ...]
    registration: AcceptedCycleRegistration | None = None

    def __post_init__(self):
        if (type(self.name) is not str or not self.name or self.name != self.name.strip()
                or not self.name.isprintable() or len(self.name) > 200):
            raise ValueError("Enter a sample name of at most 200 printable characters")
        if (type(self.members) is not tuple or not 2 <= len(self.members) <= MAX_MEMBERS
                or any(type(m) is not AlignedSourceMember for m in self.members)):
            raise ValueError(f"Group 2 to {MAX_MEMBERS} inspected images")
        if (type(self.channels) is not tuple or not 1 <= len(self.channels) <= MAX_CHANNELS
                or any(type(c) is not AlignedSourceChannel for c in self.channels)):
            raise ValueError("Invalid logical channel mapping")
        member_keys = [(m.path, m.image_index, m.series_index) for m in self.members]
        if len(set(member_keys)) != len(member_keys):
            raise ValueError("The same selected source image cannot occur twice")
        expected = {(i, c) for i, m in enumerate(self.members) for c in range(m.channel_count)}
        actual = [(c.member_index, c.channel_index) for c in self.channels]
        if len(actual) != len(expected) or set(actual) != expected:
            raise ValueError("The channel mapping must include every member channel exactly once")
        if self.registration is not None:
            from .cycle_registration import AcceptedCycleRegistration
            if (type(self.registration) is not AcceptedCycleRegistration
                    or len(self.registration.grids) != len(self.members)
                    or any(c >= m.channel_count for c, m in zip(self.registration.registration_channels, self.members))):
                raise ValueError("Registration must cover every original cycle and a valid shared-stain channel")
            cycles = [{c.imaging_cycle for c in self.channels if c.member_index == i}
                      for i in range(len(self.members))]
            if any(len(c) != 1 for c in cycles) or len(set(next(iter(c)) for c in cycles)) != len(cycles):
                raise ValueError("Register one stitched image file per distinct imaging cycle")

    def to_mapping(self):
        value = dict(schema_name=SCHEMA_NAME, schema_version=1, name=self.name,
            alignment="already_registered", members=[m.to_mapping() for m in self.members],
            channels=[c.to_mapping() for c in self.channels])
        if self.registration is not None:
            value.update(schema_version=2, alignment="registered_rigid",
                         registration=self.registration.to_mapping())
        return value

    @classmethod
    def from_mapping(cls, value):
        registered = type(value) is dict and value.get("schema_version") == 2
        fields = ("schema_name", "schema_version", "name", "alignment", "members", "channels")
        _closed(value, fields + (("registration",) if registered else ()), "collection")
        if (value["schema_name"] != SCHEMA_NAME or type(value["schema_version"]) is not int
                or (value["schema_version"], value["alignment"]) not in ((1, "already_registered"), (2, "registered_rigid"))
                or type(value["members"]) is not list or type(value["channels"]) is not list):
            raise ValueError("Unsupported aligned source collection")
        members = []
        for member in value["members"]:
            _closed(member, ("path", "image_index", "series_index", "file_sha256", "channel_count", "identity"), "member")
            identity = dict(_closed(member["identity"], ("resolved_path", "file_sha256", "size_bytes",
                "modified_time_ns", "device_id", "inode_id"), "member identity"))
            identity["resolved_path"] = Path(identity["resolved_path"])
            members.append(AlignedSourceMember(**dict(member, identity=OpenedSourceIdentity(**identity))))
        channels = tuple(AlignedSourceChannel(**_closed(c,
            ("member_index", "channel_index", "imaging_cycle"), "channel mapping")) for c in value["channels"])
        from .cycle_registration import AcceptedCycleRegistration
        registration = AcceptedCycleRegistration.from_mapping(value["registration"]) if registered else None
        return cls(value["name"], tuple(members), channels, registration)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Repeated collection JSON field")
        result[key] = value
    return result


def load_aligned_source_collection(path: str | Path) -> AlignedSourceCollection:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_DESCRIPTOR_BYTES:
        raise ValueError("Collection descriptor must be a regular file of at most 1 MiB")
    with path.open("rb") as stream:
        body = stream.read(MAX_DESCRIPTOR_BYTES + 1)
    if len(body) > MAX_DESCRIPTOR_BYTES:
        raise ValueError("Collection descriptor exceeds 1 MiB")
    return AlignedSourceCollection.from_mapping(json.loads(body, object_pairs_hook=_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite collection value"))))


def validate_collection_output(source_path: str | Path, destination: str | Path) -> None:
    """Protect every original member when publishing a logical source."""
    if not is_aligned_source_collection(source_path):
        return
    target = Path(destination).expanduser().resolve()
    for member in load_aligned_source_collection(source_path).members:
        source = member.path.resolve()
        if target == source or target.is_relative_to(source) or source.is_relative_to(target):
            raise ValueError(f"Output overlaps an original collection member: {member.path}")


def write_aligned_source_collection(path: str | Path, collection: AlignedSourceCollection) -> Path:
    """Publish a small immutable descriptor; identical retry is idempotent."""
    path = Path(path)
    if not is_aligned_source_collection(path) or type(collection) is not AlignedSourceCollection:
        raise ValueError("Aligned source descriptors require the .mif-source.json suffix")
    body = (json.dumps(collection.to_mapping(), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(body) > MAX_DESCRIPTOR_BYTES:
        raise ValueError("Collection descriptor exceeds 1 MiB")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != body:
            raise FileExistsError("A different source collection already uses this filename")
        return path.resolve()
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix=".aligned-source-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != body:
                raise
    finally:
        os.unlink(temporary)
    return path.resolve()


class AlignedCollectionSource:
    """One scalar CYX image whose bounded reads route to original members."""
    def __init__(self, path, *, backend, inspection=None, source_ome=None):
        from .canonical_ingestion import (InspectedSourceAuthority, resolve_canonical_image_selection,
                                         _validate_inspected_source_stat)
        from .aligned_collection_metadata import assemble_collection_metadata, retained_member_authorities
        self._lock = RLock()
        self._closed = False
        self._readers = OrderedDict()
        self._backend = backend
        self.collection = load_aligned_source_collection(path)
        self._identity = snapshot_opened_source_identity(Path(path),
            retained=None if inspection is None else inspection.source_identity)
        members = []
        if inspection is not None:
            members = list(retained_member_authorities(source_ome, self.collection))
            for authority in members:
                _validate_inspected_source_stat(authority.source_identity)
        else:
            for member in self.collection.members:
                snapshot_opened_source_identity(member.path, retained=member.identity)
                with backend.open(member.path) as source:
                    if source.identity.file_sha256 != member.file_sha256:
                        raise ValueError(f"Collection member changed after selection: {member.path}")
                    if source.identity != member.identity:
                        raise ValueError(f"Collection member filesystem identity changed: {member.path}")
                    selection = resolve_canonical_image_selection(source.metadata, source.series,
                        image_index=member.image_index, series_index=member.series_index,
                        require_planar_channels=True)
                    members.append(InspectedSourceAuthority.create(source_identity=source.identity,
                        canonical_ome=source.metadata, series=source.series, selection=selection,
                        backend_versions=source.versions))
        self._members = tuple(members)
        self._metadata, level, storage_bits = assemble_collection_metadata(self.collection, self._members)
        if source_ome is not None and self._metadata != source_ome:
            raise ValueError("Collection inspection metadata differs from its member authorities")
        self._series = (SeriesInfo(0, self.collection.name, (level,), storage_bits),)
        self._versions = BackendVersions("mif-aligned-source-collection/v1",
            "see-member-authorities", "see-member-authorities", "see-member-authorities")
        self._check_freshness()

    @property
    def identity(self): return self._identity

    @property
    def metadata(self): return self._metadata.model_copy(deep=True)

    @property
    def series(self): return self._series

    @property
    def versions(self): return self._versions

    def _check_freshness(self):
        from .canonical_ingestion import _validate_inspected_source_stat
        _validate_inspected_source_stat(self._identity)
        for member in self._members:
            _validate_inspected_source_stat(member.source_identity)

    def _reader(self, index):
        from .reviewed_source import _open_inspected_source
        opened = self._readers.pop(index, None)
        if opened is None:
            if len(self._readers) >= 2:
                _, oldest = self._readers.popitem(last=False)
                oldest.close()
            authority = self._members[index]
            opened = _open_inspected_source(authority, authority.canonical_ome, backend=self._backend)
        self._readers[index] = opened
        return opened

    def read_region(self, *, series, resolution, plane, region):
        with self._lock:
            if self._closed:
                raise SourceClosedError("Aligned collection is closed")
            try:
                if type(series) is not int or series != 0:
                    raise InvalidSeriesError("Aligned collection accepts series zero")
                if type(resolution) is not int or resolution != 0:
                    raise InvalidResolutionError("Aligned collection exposes full resolution only")
                if (not isinstance(plane, PlaneIndex) or plane.z != 0 or plane.t != 0
                        or plane.c >= len(self.collection.channels)):
                    raise InvalidPlaneError("Aligned collection plane is out of range")
                level = self._series[0].resolutions[0]
                if (not isinstance(region, Region) or region.x + region.width > level.size_x
                        or region.y + region.height > level.size_y):
                    raise InvalidRegionError("Aligned collection region is out of range")
                from .canonical_ingestion import _validate_inspected_source_stat
                channel = self.collection.channels[plane.c]
                authority = self._members[channel.member_index]
                _validate_inspected_source_stat(self.identity)
                _validate_inspected_source_stat(authority.source_identity)
                def read(original_region):
                    return self._reader(channel.member_index).read_region(
                        series=authority.selection.source_series_index, resolution=0,
                        plane=PlaneIndex(c=channel.channel_index), region=original_region)
                registration = self.collection.registration
                if registration is None:
                    result = read(region)
                else:
                    from .cycle_registration import resample_region
                    result = resample_region(read, registration.grids[channel.member_index],
                        registration.grids[registration.reference_member],
                        registration.transforms[channel.member_index], registration.crop, region)
                _validate_inspected_source_stat(authority.source_identity)
                if result.shape != (region.height, region.width) or result.dtype != level.dtype:
                    raise ValueError("Collection member returned a different shape or pixel type")
                return np.require(result, requirements=["C", "O"])
            except BaseException:
                self._close(False)
                raise

    def _close(self, check):
        if self._closed:
            return
        self._closed = True
        try:
            if check:
                self._check_freshness()
        finally:
            readers, self._readers = self._readers, OrderedDict()
            first_error = None
            for opened in readers.values():
                try:
                    opened.close()
                except Exception as error:
                    first_error = first_error or error
            if first_error is not None and check:
                raise first_error

    def close(self):
        with self._lock: self._close(True)

    def __enter__(self):
        if self._closed: raise SourceClosedError("Aligned collection is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        with self._lock: self._close(exc_type is None)
