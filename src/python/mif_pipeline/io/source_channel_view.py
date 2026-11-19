"""Internal reference views that retain exact original source-channel mappings.

A view changes only the exposed channel collection. Its descriptor binds the
parent inspection, accepted metadata and selected source channels. Regional reads
use the existing parent reader; no pixels are copied or registered.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path

from ome_types import from_xml, to_xml

from .backends import (InvalidPlaneError, InvalidSeriesError,
    PlaneIndex, SeriesInfo, SourceClosedError, snapshot_opened_source_identity)
from .canonical_ingestion import InspectedSourceAuthority, _validate_inspected_source_stat, validate_reviewed_ome

SUFFIX = ".mif-channel-view.json"
SCHEMA = "mif-reference-channel-view/v1"
MAX_DESCRIPTOR_BYTES = 32 * 1024 * 1024


def is_source_channel_view(path):
    return str(path).lower().endswith(SUFFIX)


def _selected_channels(values, count):
    if (type(values) is not tuple or not values or len(values) != len(set(values))
            or any(type(value) is not int or not 0 <= value < count for value in values)):
        raise ValueError("A reference view requires unique in-range source channel indices")
    return values


def selected_channel_metadata(reviewed_ome, channel_indices):
    """Project reviewed planar raw metadata without inventing acquisition facts."""
    from .metadata.canonical import (resolve_component_identity_inventory,
        resolve_unmixing_input_metadata, _encode_unmixing_input_metadata,
        apply_component_identity_inventory, ComponentIdentityMode,
        UNMIXING_INPUT_NAMESPACE, COMPONENT_IDENTITY_NAMESPACE)
    from .metadata.cycles import resolve_channel_cycles, split_cycle_component

    if len(reviewed_ome.images) != 1:
        raise ValueError("A reference channel view requires one reviewed image")
    pixels = reviewed_ome.images[0].pixels
    selected = _selected_channels(channel_indices, pixels.size_c)
    if pixels.size_z != 1 or pixels.size_t != 1 or any(c.samples_per_pixel not in (None, 1) for c in pixels.channels):
        raise ValueError("Reference views require planar channels with one Z plane and time point")
    acquisition = resolve_unmixing_input_metadata(reviewed_ome, 0)
    if acquisition is None or acquisition.spectral_state != "raw":
        raise ValueError("Reference channel views require reviewed raw measurement bands")
    inventory = resolve_component_identity_inventory(reviewed_ome, 0)
    channel_cycles = resolve_channel_cycles(reviewed_ome, require_complete=True)
    cycles = None if channel_cycles is None else {channel_cycles[index] for index in selected}
    one_cycle = cycles is not None and len(cycles) == 1
    def local_name(name):
        base, cycle = split_cycle_component(name)
        return base if one_cycle and cycle in cycles else name

    result = reviewed_ome.model_copy(deep=True)
    image = result.images[0]
    pixels = image.pixels
    pixels.channels = [pixels.channels[index] for index in selected]
    pixels.size_c = len(selected)
    positions = {original: current for current, original in enumerate(selected)}
    pixels.planes = [plane.model_copy(update={"the_c": positions[plane.the_c]})
                     for plane in pixels.planes if plane.the_c in positions]
    pixels.tiff_data_blocks = []
    pixels.bin_data_blocks = []
    vectors = {name: tuple(getattr(acquisition, name)[index] for index in selected)
               for name in ("band_order", "exposure_seconds", "gain", "binning", "binning_area", "offsets")}
    vectors["band_order"] = tuple(local_name(name) for name in vectors["band_order"])
    projected = replace(acquisition, **vectors)
    for annotation in result.structured_annotations.map_annotations:
        if annotation.namespace == UNMIXING_INPUT_NAMESPACE:
            annotation.value = _encode_unmixing_input_metadata(projected,
                channel_ids=tuple(channel.id for channel in pixels.channels))
    # Component identity is an output plan, separate from the acquired bands.
    # Filter by actual cycle; channel-bound inventories instead follow the selection.
    if inventory is not None:
        if inventory.mode is ComponentIdentityMode.PLANNED_INVENTORY:
            components = tuple(index for index, name in enumerate(inventory.component_names)
                if cycles is None or split_cycle_component(name)[1] in cycles
                or split_cycle_component(name)[1] is None)
        else:
            components = selected
        names = tuple(local_name(inventory.component_names[index]) for index in components)
        markers = (None if inventory.component_biomarkers is None else
                   tuple(inventory.component_biomarkers[index] for index in components))
        removed = {annotation.id for annotation in result.structured_annotations.map_annotations
                   if annotation.namespace == COMPONENT_IDENTITY_NAMESPACE}
        result.structured_annotations.map_annotations = [annotation
            for annotation in result.structured_annotations.map_annotations if annotation.id not in removed]
        image.annotation_refs = [reference for reference in image.annotation_refs if reference.id not in removed]
        if names:
            result = apply_component_identity_inventory(result, 0, names, mode=inventory.mode,
                promote_channel_fluorophores=False, component_biomarkers=markers,
                nuclear_stain_reviewed=inventory.nuclear_stain_reviewed,
                nuclear_component_index=(components.index(inventory.nuclear_component_index)
                    if inventory.nuclear_component_index in components else None),
                decision_source="reviewed_source_channel_view")
    return result


def write_source_channel_view(path, authority, reviewed_ome, channel_indices):
    """Publish an immutable, source-bound internal channel view descriptor."""
    if type(authority) is not InspectedSourceAuthority:
        raise TypeError("A channel view requires an inspected parent source")
    if is_source_channel_view(authority.source_identity.resolved_path):
        raise ValueError("Reference channel views cannot be nested")
    validate_reviewed_ome(authority.canonical_ome, reviewed_ome, authority.selection,
                         require_stage1_review=False)
    selected_channel_metadata(reviewed_ome, channel_indices)
    _validate_inspected_source_stat(authority.source_identity)
    path = Path(path)
    if not path.is_absolute() or not is_source_channel_view(path):
        raise ValueError("A channel view needs an absolute .mif-channel-view.json path")
    record = {"schema": SCHEMA, "parent_inspection": authority.to_json_bytes().decode("utf-8"),
              "reviewed_ome_xml": to_xml(reviewed_ome), "source_channel_indices": list(channel_indices)}
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    if len(payload) > MAX_DESCRIPTOR_BYTES:
        raise ValueError("The reference channel view descriptor exceeds its metadata limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    import os
    import tempfile
    descriptor, temporary = tempfile.mkstemp(prefix=".channel-view-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != payload:
                raise ValueError("An existing reference channel view binds different source channels")
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path.resolve()


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Repeated reference channel view field")
        result[key] = value
    return result


class SourceChannelView:
    """Read bounded regions from selected channels of one authenticated parent."""
    def __init__(self, path, *, backend, inspection=None, source_ome=None):
        from .reviewed_source import open_reviewed_source
        path = Path(path)
        self._stack = ExitStack()
        self._closed = False
        try:
            if path.is_symlink() or path.stat().st_size > MAX_DESCRIPTOR_BYTES:
                raise ValueError("Invalid reference channel view descriptor")
            self._identity = snapshot_opened_source_identity(path,
                retained=None if inspection is None else inspection.source_identity)
            record = json.loads(path.read_bytes(), object_pairs_hook=_unique_fields)
            if (type(record) is not dict or set(record) != {
                    "schema", "parent_inspection", "reviewed_ome_xml", "source_channel_indices"}
                    or record["schema"] != SCHEMA or type(record["source_channel_indices"]) is not list):
                raise ValueError("Unsupported reference channel view descriptor")
            parent = InspectedSourceAuthority.from_json_bytes(record["parent_inspection"].encode("utf-8"))
            if is_source_channel_view(parent.source_identity.resolved_path):
                raise ValueError("Reference channel views cannot be nested")
            reviewed = from_xml(record["reviewed_ome_xml"])
            self._channels = _selected_channels(tuple(record["source_channel_indices"]), reviewed.images[0].pixels.size_c)
            self._metadata = selected_channel_metadata(reviewed, self._channels)
            if source_ome is not None and self._metadata.model_dump(mode="json") != source_ome.model_dump(mode="json"):
                raise ValueError("Reference channel view metadata differs from inspection")
            self._parent = self._stack.enter_context(open_reviewed_source(parent, reviewed,
                backend=backend, require_stage1_review=False))
            series = self._parent.series[0]
            self._series = (SeriesInfo(0, series.name,
                tuple(replace(level, size_c=len(self._channels)) for level in series.resolutions), series.storage_bits),)
            self._versions = replace(self._parent.versions, backend="mif-reference-channel-view/v1")
            _validate_inspected_source_stat(self._identity)
        except BaseException:
            self._stack.close()
            self._closed = True
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
            raise SourceClosedError("Reference channel view is closed")
        try:
            if type(series) is not int or series != 0:
                raise InvalidSeriesError("Reference channel view accepts series zero")
            if (not isinstance(plane, PlaneIndex) or type(plane.c) is not int
                    or not 0 <= plane.c < len(self._channels)):
                raise InvalidPlaneError("Reference channel view plane is out of range")
            _validate_inspected_source_stat(self._identity)
            result = self._parent.read_region(series=0, resolution=resolution,
                plane=PlaneIndex(c=self._channels[plane.c], z=plane.z, t=plane.t), region=region)
            _validate_inspected_source_stat(self._identity)
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
