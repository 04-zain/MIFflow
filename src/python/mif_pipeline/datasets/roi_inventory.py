"""Read one immutable classifier ROI identity table for an admitted operation.

We retain the accepted file while hashing and projecting its typed Parquet rows.
The classifier artifact owns image, domain, model and generation association;
this module checks the exact member, bytes and four-column table representation.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from types import MappingProxyType

import pyarrow as pa
import pyarrow.parquet as pq

from mif_pipeline.io.pinned_files import PinnedFileError, PinnedReadFile, open_pinned_member

_FIELDS = frozenset({"path", "sha256", "byte_count", "row_count"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BATCH_ROWS = 8192
ROI_INVENTORY_SCHEMA = pa.schema([
    pa.field("roi_id", pa.string(), nullable=False),
    pa.field("path_object_id", pa.string(), nullable=False),
    pa.field("object_revision_sha256", pa.binary(32), nullable=False),
    pa.field("class_id", pa.string(), nullable=False),
])


class RoiInventoryError(ValueError):
    """An accepted classifier table cannot supply its committed ROI population."""


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise RoiInventoryError(f"{field} must be nonempty canonical text")
    return value


def roi_inventory_member(invocation_id: str) -> str:
    """Derive our sole table member from the authenticated producer attempt."""
    value = _text(invocation_id, "invocation_id")
    if value in {".", ".."} or any(character in value for character in "/\\:"):
        raise RoiInventoryError("invocation_id must be one safe path segment")
    return f"roi-inventories/{value}.parquet"


@dataclass(frozen=True, slots=True)
class RoiInventoryRef:
    """Commit the exact completed bytes and row count, without issuing authority."""

    path: str
    sha256: str
    byte_count: int
    row_count: int

    def __post_init__(self) -> None:
        path = _text(self.path, "ROI inventory path")
        if not path.startswith("roi-inventories/") or not path.endswith(".parquet"):
            raise RoiInventoryError("ROI inventory path is not an attempt table member")
        if path != roi_inventory_member(path[len("roi-inventories/"):-len(".parquet")]):
            raise RoiInventoryError("ROI inventory path is not canonical")
        if type(self.sha256) is not str or _SHA256.fullmatch(self.sha256) is None:
            raise RoiInventoryError("ROI inventory sha256 must be lowercase SHA-256")
        if type(self.byte_count) is not int or self.byte_count <= 0:
            raise RoiInventoryError("ROI inventory byte_count must be positive")
        if type(self.row_count) is not int or self.row_count < 0:
            raise RoiInventoryError("ROI inventory row_count must be nonnegative")

    def to_mapping(self) -> dict[str, object]:
        """Encode the closed compact descriptor for results and requests."""
        return {"path": self.path, "sha256": self.sha256,
                "byte_count": self.byte_count, "row_count": self.row_count}


def parse_roi_inventory_ref(value: object) -> RoiInventoryRef:
    """Decode descriptor metadata without reading a scientific file."""
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise RoiInventoryError("ROI inventory descriptor must contain exactly path, sha256, byte_count and row_count")
    return RoiInventoryRef(**value)


class OpenRoiInventory:
    """Keep one hash-verified Parquet reader for the caller's complete operation.

    Our first projection validates all four narrow columns and collects only
    class counts and the necessary uniqueness set. Later projections reuse the
    reader and the observed counts. Row dictionaries and whole-table buffers
    are never constructed by this owner.
    """

    def __init__(self, reference: RoiInventoryRef, pin: PinnedReadFile,
                 allowed_class_ids: frozenset[str] | None) -> None:
        self.reference = reference
        self._pin = pin
        self._allowed_class_ids = allowed_class_ids
        self._validated = False
        self._iterating = False
        self._closed = False
        self._counts: Mapping[str, int] | None = None
        self._parquet = None
        try:
            if pin.size != reference.byte_count:
                raise RoiInventoryError("ROI inventory byte count differs from its accepted descriptor")
            digest = hashlib.sha256()
            for block in pin.chunks():
                digest.update(block)
            if digest.hexdigest() != reference.sha256:
                raise RoiInventoryError("ROI inventory bytes differ from its accepted SHA-256")
            pin.stream.seek(0)
            self._parquet = pq.ParquetFile(pin.stream)
            if not self._parquet.schema_arrow.equals(ROI_INVENTORY_SCHEMA, check_metadata=False):
                raise RoiInventoryError("ROI inventory has a different typed column schema")
            if self._parquet.metadata.num_rows != reference.row_count:
                raise RoiInventoryError("ROI inventory footer count differs from its accepted descriptor")
            for index in range(self._parquet.metadata.num_row_groups):
                group = self._parquet.metadata.row_group(index)
                if group.num_rows > _BATCH_ROWS or group.total_byte_size > 8 * 1024**2:
                    raise RoiInventoryError("ROI inventory row group exceeds its bounded writer profile")
        except BaseException:
            if self._parquet is not None:
                self._parquet.close()
            pin.close()
            raise

    def iter_batches(self, *, columns: Sequence[str]) -> Iterator[pa.RecordBatch]:
        """Project requested columns in bounded batches from our retained bytes.

        The first complete scan admits sorted unique ROI IDs, unique native
        object IDs, nonempty class names and binary32 revisions. Partial scans
        cannot establish a valid inventory; closing drains the ingress check.
        """
        requested = tuple(columns)
        if not requested or len(set(requested)) != len(requested) or not set(requested) <= set(ROI_INVENTORY_SCHEMA.names):
            raise RoiInventoryError("ROI inventory projection requires distinct known columns")
        if self._closed or self._iterating:
            raise RoiInventoryError("ROI inventory reader is closed or already projecting")
        self._iterating = True
        ingress = not self._validated
        previous_id = None
        path_ids: set[str] = set()
        counts: Counter[str] = Counter()
        total = 0
        try:
            projected = ROI_INVENTORY_SCHEMA.names if ingress else list(requested)
            for batch in self._parquet.iter_batches(batch_size=_BATCH_ROWS, columns=projected, use_threads=False):
                if ingress:
                    if any(column.null_count for column in batch.columns):
                        raise RoiInventoryError("ROI inventory contains null fields")
                    # Scalar access avoids four additional whole-batch Python lists.
                    for index in range(batch.num_rows):
                        roi_id = _text(batch.column(0)[index].as_py(), "roi_id")
                        path_id = _text(batch.column(1)[index].as_py(), "path_object_id")
                        class_id = _text(batch.column(3)[index].as_py(), "class_id")
                        if previous_id is not None and roi_id <= previous_id:
                            raise RoiInventoryError("ROI inventory roi_id order is not strictly ascending")
                        if path_id in path_ids:
                            raise RoiInventoryError("ROI inventory repeats a native path_object_id")
                        if len(batch.column(2)[index].as_py()) != 32:
                            raise RoiInventoryError("ROI inventory revision is not binary32")
                        if self._allowed_class_ids is not None and class_id not in self._allowed_class_ids:
                            raise RoiInventoryError("ROI inventory class differs from its admitted vocabulary")
                        previous_id = roi_id
                        path_ids.add(path_id)
                        counts[class_id] += 1
                    total += batch.num_rows
                    yield batch.select(requested)
                else:
                    yield batch
            if ingress:
                if total != self.reference.row_count:
                    raise RoiInventoryError("ROI inventory decoded count differs from its descriptor")
                self._counts = MappingProxyType(dict(counts))
                self._validated = True
        except (pa.ArrowException, OSError) as exc:
            raise RoiInventoryError(f"ROI inventory Parquet projection failed: {exc}") from exc
        finally:
            self._iterating = False

    def class_counts(self) -> Mapping[str, int]:
        """Return table class counts, retaining Ignore for the stage owner to select."""
        if self._closed:
            raise RoiInventoryError("ROI inventory reader is closed")
        if not self._validated:
            for _batch in self.iter_batches(columns=["class_id"]):
                pass
        return self._counts

    def close(self) -> None:
        """Finish ingress and the retained-file check before releasing the reader."""
        if self._closed:
            return
        try:
            if not self._validated:
                self.class_counts()
            self._pin.validate()
        except PinnedFileError as exc:
            raise RoiInventoryError(str(exc)) from exc
        finally:
            self._closed = True
            try:
                self._parquet.close()
            finally:
                self._pin.close()

    def __enter__(self) -> OpenRoiInventory:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            self.close()
        else:
            # A failed consumer operation cannot publish the table; release its
            # bytes without a redundant scan masking the original failure.
            self._closed = True
            try:
                self._parquet.close()
            finally:
                self._pin.close()


def open_roi_inventory(reference: RoiInventoryRef, *, admitted_root: Path,
                       expected_member: str,
                       allowed_class_ids: Iterable[str] | None = None) -> OpenRoiInventory:
    """Admit the exact producer member before opening its retained table bytes."""
    if type(reference) is not RoiInventoryRef or reference.path != expected_member:
        raise RoiInventoryError("ROI inventory member differs from its admitted producer attempt")
    allowed = None
    if allowed_class_ids is not None:
        allowed = frozenset(_text(value, "allowed class ID") for value in allowed_class_ids)
        if not allowed:
            raise RoiInventoryError("ROI inventory requires a nonempty admitted class vocabulary")
    try:
        pin = open_pinned_member(Path(admitted_root), expected_member)
        return OpenRoiInventory(reference, pin, allowed)
    except (PinnedFileError, pa.ArrowException, OSError) as exc:
        raise RoiInventoryError(f"ROI inventory cannot retain its admitted table: {exc}") from exc
