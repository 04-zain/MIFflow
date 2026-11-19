"""A reviewed planar view of exact original acquisition-plane addresses.

The original inspection remains immutable in the descriptor. The view changes
only the exposed channel inventory; the reader still resolves the selected
source series, C, T and Z. It never calculates physical TIFF plane positions.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
from xml.etree import ElementTree as ET

from ome_types import from_xml, to_xml
from ome_types.model import AnnotationRef, MapAnnotation, StructuredAnnotations

from .backends import (InvalidPlaneError, InvalidSeriesError, PlaneIndex,
    SeriesInfo, SourceClosedError, snapshot_opened_source_identity)
from .canonical_ingestion import (InspectedSourceAuthority, _analytical_pair_reasons,
    _validate_inspected_source_stat, selected_canonical_ome)
from .metadata.cycles import apply_channel_cycle_review, resolve_channel_cycles

SUFFIX = ".mif-plane-view.json"
SCHEMA = "mif-acquired-plane-view/v1"
MAX_DESCRIPTOR_BYTES = 32 * 1024 * 1024
MAX_PLANES = 512
PROVENANCE_NAMESPACE = "urn:mif-pipeline:acquired-plane:v1"


def is_acquired_plane_view(path):
    return str(path).lower().endswith(SUFFIX)


def validate_source_dimensions(ome, image_index, series):
    """Admit only a scalar complete Z1 source; this does not approve T semantics."""
    reasons = _analytical_pair_reasons(ome, image_index, series,
        require_planar_channels=True)
    pixels = ome.images[image_index].pixels
    base = series.resolutions[0]
    if (set(reasons) - {"eligible_analytical_series", "unsupported_z_or_time"}
            or pixels.size_z != 1 or base.size_z != 1
            or pixels.size_t != base.size_t or pixels.size_c * pixels.size_t > MAX_PLANES
            or any((level.size_z, level.size_c, level.size_t, level.rgb_samples, level.dtype)
                   != (1, base.size_c, base.size_t, 1, base.dtype)
                   for level in series.resolutions)):
        raise ValueError("Cycle organization requires one scalar Z=1 image with at most 512 acquired planes")
    validate_linked_modulo(to_xml(ome), image_index)
    return pixels


def validate_linked_modulo(xml, image_index):
    """Check selected Image/Pixels-linked Modulo declarations before projection.

    No Modulo semantic adapter is implemented. Cardinality and axis checks
    provide a precise failure before the unsupported meaning can be discarded.
    Unrelated annotations belonging to another selected image are not consumed.
    """
    root = ET.fromstring(xml)
    local = lambda tag: tag.rsplit("}", 1)[-1]
    images = [element for element in root if local(element.tag) == "Image"]
    image = images[image_index]
    pixels = next(element for element in image if local(element.tag) == "Pixels")
    refs = {element.attrib.get("ID") for parent in (image, pixels)
            for element in parent if local(element.tag) == "AnnotationRef"}
    annotations = [element for element in root.iter()
                   if local(element.tag) == "XMLAnnotation" and element.attrib.get("ID") in refs]
    for annotation in annotations:
        for element in annotation.iter():
            name = local(element.tag)
            if name not in {"ModuloAlongC", "ModuloAlongT", "ModuloAlongZ"}:
                continue
            labels = [child for child in element if local(child.tag) == "Label"]
            if labels:
                count = len(labels)
            else:
                try:
                    start, end, step = (float(element.attrib[key]) for key in ("Start", "End", "Step"))
                    cardinality = (end - start) / step + 1
                    if (not all(math.isfinite(value) for value in (start, end, step, cardinality))
                            or step <= 0 or cardinality <= 0 or not cardinality.is_integer()):
                        raise ValueError
                    count = int(cardinality)
                except (KeyError, ValueError, ZeroDivisionError) as exc:
                    raise ValueError("Linked OME Modulo has invalid cardinality") from exc
            axis_size = int(pixels.attrib["Size" + name[-1]])
            if count <= 0 or axis_size % count:
                raise ValueError("Linked OME Modulo cardinality does not divide its source axis")
            raise ValueError("This image has a linked OME Modulo dimension whose dimension meaning is unsupported. "
                             "Export a registered cycle/channel image with explicit cycle and channel organization.")


def _parent_pixels(authority):
    if type(authority) is not InspectedSourceAuthority or is_acquired_plane_view(authority.source_identity.resolved_path):
        raise ValueError("An acquired-plane view requires one original inspected source")
    selection = authority.selection
    ome = authority.canonical_ome
    pixels = validate_source_dimensions(ome, selection.source_image_index,
        authority.series[selection.source_series_index])
    image = ome.images[selection.source_image_index]
    if (image.id != selection.image_id or pixels.id != selection.pixels_id
            or authority.series[selection.source_series_index].name != selection.series_name):
        raise ValueError("Original source selection differs from its inspection")
    return pixels


def build_plane_rows(authority, cycles):
    """Bind a complete T-major view, or preserve a planar source's C order."""
    pixels = _parent_pixels(authority)
    if not isinstance(cycles, (tuple, list)) or len(cycles) != (pixels.size_t if pixels.size_t > 1 else pixels.size_c):
        raise ValueError("Cycle organization must cover every source T or planar channel")
    return [dict(source_series_index=authority.selection.source_series_index,
        source_c=c, source_t=t, source_z=0, source_channel_id=channel.id,
        channel_id=f"Channel:AcquiredPlane:{t * pixels.size_c + c}" if pixels.size_t > 1 else channel.id,
        cycle=cycles[t] if pixels.size_t > 1 else cycles[c])
        for t in range(pixels.size_t) for c, channel in enumerate(pixels.channels)]


def _validate_rows(authority, rows, decision):
    pixels = _parent_pixels(authority)
    if (not isinstance(decision, Mapping) or set(decision) != {"origin", "layout", "registered"}
            or decision["origin"] != "researcher" or type(decision["registered"]) is not bool
            or decision["layout"] != ("t_cycles" if pixels.size_t > 1 else "planar_channels")):
        raise ValueError("Explicit researcher review must establish the source cycle and channel layout")
    if pixels.size_t > 1 and not decision["registered"]:
        raise ValueError("T-encoded cycles must already be registered to one spatial grid. "
                         "Register the source first, or use the existing separate-cycle registration workflow.")
    if not isinstance(rows, (list, tuple)) or len(rows) != pixels.size_c * pixels.size_t:
        raise ValueError("The acquired-plane view must include the complete C by T source grid")
    cycles = []
    keys = {"source_series_index", "source_c", "source_t", "source_z", "source_channel_id", "channel_id", "cycle"}
    for v, row in enumerate(rows):
        t, c = divmod(v, pixels.size_c)
        expected = (authority.selection.source_series_index, c, t, 0)
        if (not isinstance(row, Mapping) or set(row) != keys
                or any(type(row[key]) is not int for key in ("source_series_index", "source_c", "source_t", "source_z"))
                or tuple(row[key] for key in ("source_series_index", "source_c", "source_t", "source_z")) != expected
                or row["source_channel_id"] != pixels.channels[c].id
                or row["channel_id"] != (f"Channel:AcquiredPlane:{v}" if pixels.size_t > 1 else pixels.channels[c].id)):
            raise ValueError("Acquired-plane addresses and channel IDs must retain the complete original source order")
        cycle = row["cycle"]
        if (cycle is None and pixels.size_t > 1) or (cycle is not None and (type(cycle) is not int or cycle < 0)):
            raise ValueError("An acquired-plane cycle must be a nonnegative integer; only planar references may remain unknown")
        cycles.append(cycle)
    if pixels.size_t > 1:
        groups = [set(cycles[t * pixels.size_c:(t + 1) * pixels.size_c]) for t in range(pixels.size_t)]
        if any(len(group) != 1 for group in groups) or len(set(cycles)) != pixels.size_t:
            raise ValueError("T-encoded organization requires one distinct imaging cycle per original T")
        if resolve_channel_cycles(authority.canonical_ome, authority.selection.source_image_index) is not None:
            raise ValueError("Channel-bound cycles conflict with a simple T-as-cycles layout; mixed cycle and channel organization is unsupported")
    return pixels


def project_plane_metadata(authority, rows, decision):
    """Project exact plane facts, leaving biological identity for channel review."""
    original = _validate_rows(authority, rows, decision)
    result = selected_canonical_ome(authority.canonical_ome, authority.selection)
    image = result.images[0]
    if original.size_t == 1:
        return apply_channel_cycle_review(result, [dict(channel_id=row["channel_id"], cycle=row["cycle"]) for row in rows])
    # Source C-bound annotation inventories have no demonstrated per-T scope.
    # Preserve them in the embedded original inspection, never repeat them.
    result.structured_annotations = StructuredAnnotations()
    # Source ROIs can address original C/T positions. Retain them in the parent
    # without attaching their unchanged coordinates to the different view axes.
    result.rois, image.roi_refs = [], []
    def clear_annotations(value):
        if hasattr(value, "annotation_refs"):
            value.annotation_refs = []
        if hasattr(type(value), "model_fields"):
            for field in type(value).model_fields:
                child = getattr(value, field)
                if isinstance(child, list):
                    for item in child:
                        clear_annotations(item)
                elif hasattr(type(child), "model_fields"):
                    clear_annotations(child)
    clear_annotations(result)
    pixels = image.pixels
    records = {}
    for plane in original.planes:
        address = (plane.the_c, plane.the_t, plane.the_z)
        if address in records:
            raise ValueError("Source has duplicate or conflicting Plane metadata for one C/T/Z address")
        if not (0 <= plane.the_c < original.size_c and 0 <= plane.the_t < original.size_t and plane.the_z == 0):
            raise ValueError("Source Plane metadata is outside the selected C/T/Z grid")
        records[address] = plane
    pixels.channels = []
    pixels.planes = []
    pixels.size_c, pixels.size_t, pixels.size_z = len(rows), 1, 1
    pixels.dimension_order = "XYCZT"
    pixels.tiff_data_blocks, pixels.bin_data_blocks = [], []
    pixels.metadata_only = None
    for v, row in enumerate(rows):
        channel = original.channels[row["source_c"]].model_copy(deep=True)
        channel.id, channel.annotation_refs = row["channel_id"], []
        pixels.channels.append(channel)
        plane = records.get((row["source_c"], row["source_t"], row["source_z"]))
        if plane is not None:
            projected = plane.model_copy(deep=True)
            projected.the_c, projected.the_t, projected.the_z = v, 0, 0
            projected.annotation_refs = []
            pixels.planes.append(projected)
        annotation = MapAnnotation(id=f"Annotation:AcquiredPlane:{v}", namespace=PROVENANCE_NAMESPACE,
            value={key: str(value) for key, value in row.items() if key != "cycle"})
        result.structured_annotations.map_annotations.append(annotation)
        channel.annotation_refs.append(AnnotationRef(id=annotation.id))
    result = apply_channel_cycle_review(result, [dict(channel_id=row["channel_id"], cycle=row["cycle"]) for row in rows])
    from .metadata.canonical import canonicalize_ome
    return canonicalize_ome(result, source_image_indices=(0,))


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Repeated acquired-plane descriptor field")
        result[key] = value
    return result


def _validate_descriptor_identity(identity):
    """Hash this small authority file even when its size and timestamp match."""
    _validate_inspected_source_stat(identity)
    path = identity.resolved_path
    if path.is_symlink() or path.stat().st_size > MAX_DESCRIPTOR_BYTES:
        raise ValueError("Invalid acquired-plane descriptor")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != identity.file_sha256:
        raise ValueError("Acquired-plane descriptor differs from its retained identity")
    _validate_inspected_source_stat(identity)
    return payload


def read_plane_descriptor(path, *, inspection=None):
    """Authenticate the immutable descriptor and recompute its projected facts."""
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > MAX_DESCRIPTOR_BYTES:
        raise ValueError("Invalid acquired-plane descriptor")
    identity = snapshot_opened_source_identity(path,
        retained=None if inspection is None else inspection.source_identity)
    record = json.loads(_validate_descriptor_identity(identity), object_pairs_hook=_unique_fields)
    if (type(record) is not dict or set(record) != {"schema", "parent_inspection", "decision", "rows", "view_ome_xml"}
            or record["schema"] != SCHEMA or type(record["parent_inspection"]) is not str
            or type(record["view_ome_xml"]) is not str):
        raise ValueError("Unsupported acquired-plane descriptor")
    parent = InspectedSourceAuthority.from_json_bytes(record["parent_inspection"].encode("utf-8"))
    metadata = project_plane_metadata(parent, record["rows"], record["decision"])
    if metadata.model_dump(mode="json") != from_xml(record["view_ome_xml"]).model_dump(mode="json"):
        raise ValueError("Acquired-plane metadata differs from its original source addresses")
    _validate_inspected_source_stat(parent.source_identity)
    _validate_inspected_source_stat(identity)
    return parent, record, metadata, identity


def write_acquired_plane_view(path, authority, rows, decision):
    """Publish a reviewed view without modifying source pixels or metadata."""
    metadata = project_plane_metadata(authority, rows, decision)
    _validate_inspected_source_stat(authority.source_identity)
    path = Path(path)
    if not path.is_absolute() or not is_acquired_plane_view(path):
        raise ValueError("An acquired-plane view needs an absolute .mif-plane-view.json path")
    record = dict(schema=SCHEMA, parent_inspection=authority.to_json_bytes().decode("utf-8"),
        decision=dict(decision), rows=[dict(row) for row in rows], view_ome_xml=to_xml(metadata))
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    if len(payload) > MAX_DESCRIPTOR_BYTES:
        raise ValueError("Acquired-plane descriptor exceeds its metadata limit")
    import os
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".plane-view-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != payload:
                raise ValueError("An existing acquired-plane view binds a different organization")
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path.resolve()


class AcquiredPlaneView:
    """Expose scalar source planes as planar C while delegating original addresses."""
    def __init__(self, path, *, backend, inspection=None, source_ome=None):
        from .reviewed_source import _open_inspected_source
        self._stack, self._closed = ExitStack(), False
        try:
            parent, record, metadata, self._identity = read_plane_descriptor(path, inspection=inspection)
            self._parent_authority = parent
            self._rows, self._metadata = record["rows"], metadata
            if source_ome is not None and metadata.model_dump(mode="json") != source_ome.model_dump(mode="json"):
                raise ValueError("Acquired-plane view metadata differs from inspection")
            self._parent = self._stack.enter_context(_open_inspected_source(parent, parent.canonical_ome, backend=backend))
            selected = self._parent.series[parent.selection.source_series_index]
            self._series = (SeriesInfo(0, selected.name, tuple(replace(level,
                size_c=len(self._rows), size_t=1, size_z=1, dimension_order="XYCZT")
                for level in selected.resolutions), selected.storage_bits),)
            self._versions = replace(parent.backend_versions, backend=SCHEMA)
            _validate_descriptor_identity(self._identity)
        except BaseException:
            self.close()
            raise

    @property
    def identity(self): return self._identity

    @property
    def metadata(self): return self._metadata.model_copy(deep=True)

    @property
    def series(self): return self._series

    @property
    def versions(self): return self._versions

    def read_region(self, *, series, resolution, plane, region):
        if self._closed:
            raise SourceClosedError("Acquired-plane view is closed")
        try:
            if type(series) is not int or series != 0:
                raise InvalidSeriesError("Acquired-plane view accepts series zero")
            if (not isinstance(plane, PlaneIndex) or plane.t != 0 or plane.z != 0
                    or not 0 <= plane.c < len(self._rows)):
                raise InvalidPlaneError("Acquired-plane view requires virtual T=0, Z=0 and a valid channel")
            _validate_descriptor_identity(self._identity)
            _validate_inspected_source_stat(self._parent_authority.source_identity)
            row = self._rows[plane.c]
            result = self._parent.read_region(series=row["source_series_index"], resolution=resolution,
                plane=PlaneIndex(c=row["source_c"], t=row["source_t"], z=row["source_z"]), region=region)
            _validate_inspected_source_stat(self._parent_authority.source_identity)
            _validate_descriptor_identity(self._identity)
            return result
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self._closed:
            self._closed = True
            self._stack.close()

    def __enter__(self): return self

    def __exit__(self, *_): self.close()
