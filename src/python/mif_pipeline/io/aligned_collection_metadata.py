"""Combine inspected member metadata without inventing acquisition facts."""
from __future__ import annotations

from dataclasses import replace, fields
import json
import math

from ome_types.model import AnnotationRef, Detector, MapAnnotation, OME, StructuredAnnotations
from pydantic import BaseModel

from .canonical_ingestion import InspectedSourceAuthority, selected_canonical_ome, _canonical_json_bytes
from .canonical_ome_tiff import _physical_calibration
from .metadata import canonical as canonical
from .metadata import calibration
from .metadata.cycles import SOURCE_CHANNEL_NAMESPACE, resolve_channel_cycles, cycle_component_name


def _walk(value):
    if isinstance(value, BaseModel):
        yield value
        for name in type(value).model_fields:
            yield from _walk(getattr(value, name))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)


def _instrument_bindings(omes):
    """Normalize OME object IDs; fixed instrument facts must agree."""
    original = None
    maps = []
    target_ids = None
    for ome in omes:
        instruments = [item.model_copy(deep=True) for item in ome.instruments]
        ids = list(dict.fromkeys(node.id for item in instruments for node in _walk(item)
                                if hasattr(node, "id")))
        mapping = {value: f"{value.split(':')[0]}:{index}" for index, value in enumerate(ids)}
        for item in instruments:
            for node in _walk(item):
                if hasattr(node, "id"):
                    node.id = mapping[node.id]
                if isinstance(node, Detector):
                    # These are intensity calibration values, not optical layout.
                    # Their source values are copied to the corresponding channel.
                    node.gain = None
                    node.offset = None
        normalized = [item.model_dump(mode="json") for item in instruments]
        if original is None:
            original, target_ids = normalized, ids
        if normalized != original:
            raise ValueError("Aligned files must describe the same fixed instrument and optical settings")
        maps.append(dict(zip(ids, target_ids, strict=True)))
    return maps


def retained_member_authorities(ome, collection):
    from .aligned_collection import COLLECTION_NAMESPACE
    refs = {r.id for r in ome.images[0].annotation_refs}
    annotations = [] if ome.structured_annotations is None else [a for a in
        ome.structured_annotations.map_annotations if a.id in refs and a.namespace == COLLECTION_NAMESPACE]
    if len(annotations) != 1:
        raise ValueError("Collection inspection is missing its member provenance")
    value = dict(annotations[0].value)
    if set(value) != {"descriptor", "member_authorities"} or json.loads(value["descriptor"]) != collection.to_mapping():
        raise ValueError("Collection descriptor differs from its inspected channel mapping")
    members = tuple(InspectedSourceAuthority.from_json_bytes(_canonical_json_bytes(m))
                    for m in json.loads(value["member_authorities"]))
    if len(members) != len(collection.members):
        raise ValueError("Collection inspection does not cover every member")
    for declared, inspected in zip(collection.members, members, strict=True):
        if (declared.path != inspected.source_identity.resolved_path
                or declared.identity != inspected.source_identity
                or declared.file_sha256 != inspected.source_identity.file_sha256
                or declared.image_index != inspected.selection.source_image_index
                or declared.series_index != inspected.selection.source_series_index):
            raise ValueError("Collection member identity or image selection differs from inspection")
    return members


def _acquisition(ome):
    complete = canonical.resolve_unmixing_input_metadata(ome, 0)
    pending = calibration._find(ome, 0, calibration.PENDING_NS) or calibration._extract(ome, 0)
    if complete is not None:
        pending = dict(pending, band_names=list(complete.band_order),
            dtype=complete.source_storage_dtype, storage_bits=complete.source_storage_bits,
            storage_max=complete.source_storage_max,
            spectral_state=complete.spectral_state, autofluorescence_subtracted=complete.autofluorescence_subtracted,
            intensity_representation=complete.intensity_representation,
            offset_authority=complete.offset_authority.value,
            values={name: list(getattr(complete, name)) for name in (*calibration.FIELD_INFO, "camera_bit_depth")})
    from .metadata.spectral import resolve_spectral_state, _resolve_source_spectral_state
    state, _ = _resolve_source_spectral_state(ome, 0,
        spectral=resolve_spectral_state(ome, 0), acquisition=complete)
    pending["spectral_state"] = state
    return complete, pending


def _merge_acquisition(ome, collection, members):
    pairs = [_acquisition(member) for member in members]
    complete = [pair[0] for pair in pairs]
    data = [pair[1] for pair in pairs]
    common = ("dtype", "storage_bits", "storage_max", "spectral_state",
              "autofluorescence_subtracted", "intensity_representation", "offset_authority")
    for name in common:
        if any(member[name] != data[0][name] for member in data[1:]):
            raise ValueError(f"Aligned source imaging settings disagree: {name}")
    result = {name: data[0][name] for name in common}
    result["channel_ids"] = [c.id for c in ome.images[0].pixels.channels]
    result["band_names"] = [cycle_component_name(data[c.member_index]["band_names"][c.channel_index], c.imaging_cycle)
                            for c in collection.channels]
    result["values"] = {name: [data[c.member_index]["values"][name][c.channel_index]
        for c in collection.channels] for name in calibration.FIELD_INFO}
    camera = [row["values"]["camera_bit_depth"] for row in data]
    result["values"]["camera_bit_depth"] = (list(dict.fromkeys(v for row in camera for v in row))
                                             if all(camera) else [])
    calibration._put(ome, 0, calibration.PENDING_NS, "AcquisitionInventory", result)
    if all(item is not None for item in complete):
        vector_fields = {"band_order", "exposure_seconds", "gain", "binning", "binning_area", "offsets"}
        varying = vector_fields | {"image_index", "state_source", "camera_bit_depth"}
        for field in fields(complete[0]):
            if field.name not in varying and any(getattr(item, field.name) != getattr(complete[0], field.name) for item in complete[1:]):
                raise ValueError(f"Aligned source intensity-correction conventions differ: {field.name}")
        updates = {name: tuple(getattr(complete[c.member_index], name)[c.channel_index]
            for c in collection.channels) for name in vector_fields - {"band_order"}}
        combined = replace(complete[0], image_index=0, state_source="aligned_source_members",
            band_order=tuple(result["band_names"]), camera_bit_depth=tuple(result["values"]["camera_bit_depth"]), **updates)
        annotation = MapAnnotation(id="Annotation:UnmixingInput:0", namespace=canonical.UNMIXING_INPUT_NAMESPACE,
            value=canonical._encode_unmixing_input_metadata(combined, channel_ids=tuple(result["channel_ids"])))
        ome.structured_annotations.map_annotations.append(annotation)
        ome.images[0].annotation_refs.append(AnnotationRef(id=annotation.id))
    elif any(item is not None and (item.offset_profile_identifier is not None
             or item.acquisition_compatibility_profile_id is not None) for item in complete):
        raise ValueError("Complete each member's imaging-settings review before combining reviewed calibration profiles")
    else:
        calibration.retain_incomplete_acquisition(ome, 0)


def _retain_registration_acquisition(output, authorities, omes):
    """Keep original count-range evidence when spatial samples become floating point."""
    import hashlib
    evidence = []
    for authority, ome in zip(authorities, omes, strict=True):
        pixels = ome.images[0].pixels
        storage = authority.series[authority.selection.source_series_index].storage_bits
        bits = pixels.significant_bits or (storage.bits if storage is not None else None)
        if bits is None:
            return  # Absence remains absence; never infer camera bits from resampling.
        evidence.append(bits)
    if len(set(evidence)) != 1:
        raise ValueError("Registered measurement cycles require matching original count-range evidence")
    pending = calibration._find(output, 0, calibration.PENDING_NS)
    complete = canonical.resolve_unmixing_input_metadata(output, 0)
    if pending is None or pending["intensity_representation"] == "normalized_counts":
        return
    import numpy as np
    if np.dtype(pending["dtype"]).kind != "u":
        return
    bits = evidence[0]
    proof = json.dumps(dict(sources=[a.source_identity.file_sha256 for a in authorities],
                           original_meaningful_bits=bits), sort_keys=True).encode()
    values = dict(digital_range_profile_id="registered-cycle-original-range:" + hashlib.sha256(proof).hexdigest(),
                  digital_range_meaningful_bits=bits, digital_range_maximum=float((1 << bits)-1))
    # Preserve an existing reviewed range profile when it is present.
    if complete is not None and complete.digital_range_profile_id is not None:
        return
    for annotation in output.structured_annotations.map_annotations:
        if annotation.namespace == calibration.PENDING_NS:
            annotation.value = {"record": calibration._json(dict(pending, **values))}
    if complete is not None:
        updated = replace(complete, **values)
        for annotation in output.structured_annotations.map_annotations:
            if annotation.namespace == canonical.UNMIXING_INPUT_NAMESPACE:
                annotation.value = canonical._encode_unmixing_input_metadata(updated,
                    channel_ids=tuple(c.id for c in output.images[0].pixels.channels))


def _xy_calibration(pixels):
    return (_physical_calibration(pixels.physical_size_x, pixels.physical_size_x_unit, "X")[2],
            _physical_calibration(pixels.physical_size_y, pixels.physical_size_y_unit, "Y")[2])


def _retain_processing_and_components(output, collection, omes):
    from .metadata.spectral import resolve_spectral_state, SPECTRAL_PROCESSING_NAMESPACE
    from .metadata.canonical import ComponentIdentityMode
    spectra = [resolve_spectral_state(ome, 0) for ome in omes]
    acquisition = calibration._find(output, 0, calibration.PENDING_NS)
    names = []
    for member, spectral in zip(omes, spectra, strict=True):
        inventory = canonical.resolve_component_identity_inventory(member, 0)
        if inventory is not None and inventory.mode is ComponentIdentityMode.CHANNEL_FLUOROPHORES:
            names.append(inventory.component_names)
        elif spectral.state == "unmixed" and spectral.component_order is not None:
            names.append(spectral.component_order)
        else:
            names.append(None)
    component_names = None
    if all(item is not None for item in names):
        component_names = tuple(cycle_component_name(names[c.member_index][c.channel_index], c.imaging_cycle)
                                for c in collection.channels)
        rows = canonical.resolve_channel_metadata(output, 0)
        markers = (tuple(row.biomarker for row in rows)
                   if all(row.biomarker_assignment != "missing" for row in rows) else None)
        output = canonical.apply_component_identity_inventory(output, 0, component_names,
            mode=ComponentIdentityMode.CHANNEL_FLUOROPHORES, promote_channel_fluorophores=False,
            component_biomarkers=markers, decision_source="aligned_source_members")
    state = acquisition["spectral_state"]
    if state == "raw" or (state == "unmixed" and component_names is not None):
        af = acquisition["autofluorescence_subtracted"]
        value = dict(spectral_state=state, is_unmixed_component=str(state == "unmixed").lower(),
            autofluorescence_subtracted="unknown" if af is None else str(af).lower(),
            declaration_source="aligned_source_members")
        if state == "unmixed":
            value["component_order"] = json.dumps(component_names, separators=(",", ":"))
        annotation = MapAnnotation(id="Annotation:AlignedSpectralState:0",
            namespace=SPECTRAL_PROCESSING_NAMESPACE, value=value)
        output.structured_annotations.map_annotations.append(annotation)
        output.images[0].annotation_refs.append(AnnotationRef(id=annotation.id))
    return output


def assemble_collection_metadata(collection, authorities):
    from .aligned_collection import COLLECTION_NAMESPACE
    omes = [selected_canonical_ome(a.canonical_ome, a.selection) for a in authorities]
    registration = collection.registration
    reference = 0 if registration is None else registration.reference_member
    base = omes[reference].images[0].pixels
    base_level = authorities[reference].series[authorities[reference].selection.source_series_index].resolutions[0]
    base_storage = authorities[0].series[authorities[0].selection.source_series_index].storage_bits
    base_range_bits = base.significant_bits or (base_storage.bits if base_storage is not None
                                                else base_level.dtype.itemsize * 8)
    calibration_pair = _xy_calibration(base)
    if any(v is None or not math.isfinite(v) or v <= 0 for v in calibration_pair):
        raise ValueError("Already aligned files require known positive XY pixel calibration")
    for member, ome, authority in zip(collection.members, omes, authorities, strict=True):
        pixels = ome.images[0].pixels
        level = authority.series[authority.selection.source_series_index].resolutions[0]
        if pixels.size_c != member.channel_count or len(pixels.channels) != member.channel_count:
            raise ValueError("Collection member channel inventory changed")
        if (pixels.size_z, pixels.size_t, level.rgb_samples) != (1, 1, 1):
            raise ValueError("Aligned collections require planar scalar fluorescence with Z=T=1")
        if registration is None and (pixels.size_x, pixels.size_y) != (base.size_x, base.size_y):
            raise ValueError("Already aligned files require identical XY image dimensions")
        current = _xy_calibration(pixels)
        if registration is None and any(a is None or not math.isclose(a, b, rel_tol=1e-9, abs_tol=0.)
               for a, b in zip(current, calibration_pair, strict=True)):
            raise ValueError("Already aligned files require matching XY pixel calibration")
        if pixels.type != base.type or pixels.significant_bits != base.significant_bits or level.dtype != base_level.dtype:
            raise ValueError("Aligned files require the same pixel type and meaningful bit depth")
        storage = authority.series[authority.selection.source_series_index].storage_bits
        range_bits = pixels.significant_bits or (storage.bits if storage is not None else level.dtype.itemsize * 8)
        if range_bits != base_range_bits:
            raise ValueError("Aligned files require the same meaningful stored intensity range")
    if registration is not None:
        from .cycle_registration import CycleGrid
        actual = tuple(CycleGrid(o.images[0].pixels.size_x, o.images[0].pixels.size_y,
                                 *_xy_calibration(o.images[0].pixels)) for o in omes)
        if any((a.width,a.height) != (b.width,b.height)
                or not math.isclose(a.x_um,b.x_um,rel_tol=1e-9,abs_tol=0.)
                or not math.isclose(a.y_um,b.y_um,rel_tol=1e-9,abs_tol=0.)
                for a,b in zip(actual,registration.grids,strict=True)):
            raise ValueError("Original cycle geometry or pixel calibration changed after registration")
    instrument_maps = _instrument_bindings(omes)
    image = omes[reference].images[0].model_copy(deep=True)
    for node in _walk(image):
        if hasattr(node, "id") and node.id in instrument_maps[reference]:
            node.id = instrument_maps[reference][node.id]
    image.id, image.name = "Image:0", collection.name
    image.description = ("Logical channel collection; input files declared already registered." if registration is None else
        "Registered cycle collection; original pixels sampled once with linear interpolation on the common reference crop.")
    image.annotation_refs = []
    image.roi_refs = []
    image.acquisition_date = None
    pixels = image.pixels
    pixels.id, pixels.size_c, pixels.dimension_order = "Pixels:0", len(collection.channels), "XYCZT"
    pixels.channels, pixels.planes, pixels.tiff_data_blocks, pixels.bin_data_blocks = [], [], [], []
    pixels.metadata_only = None
    output = OME(images=[image], instruments=[item.model_copy(deep=True) for item in omes[0].instruments],
                 structured_annotations=StructuredAnnotations())
    instrument_refs = {node.id for item in output.instruments for node in _walk(item)
                       if isinstance(node, AnnotationRef)}
    if omes[0].structured_annotations is not None:
        for field in StructuredAnnotations.model_fields:
            getattr(output.structured_annotations, field).extend(a.model_copy(deep=True)
                for a in getattr(omes[0].structured_annotations, field) if a.id in instrument_refs)
    source_cycles = [resolve_channel_cycles(ome) for ome in omes]
    for global_c, mapping in enumerate(collection.channels):
        member_index, local = mapping.member_index, mapping.channel_index
        original = omes[member_index]
        channel = original.images[0].pixels.channels[local].model_copy(deep=True)
        original_id = channel.id
        if (source_cycles[member_index] is not None and source_cycles[member_index][local] is not None
                and source_cycles[member_index][local] != mapping.imaging_cycle):
            raise ValueError("Entered cycle conflicts with the member's recorded imaging cycle")
        if channel.detector_settings is not None:
            for attribute, field in (("gain", "gain"), ("offset", "offsets")):
                if getattr(channel.detector_settings, attribute) is None:
                    value = calibration._channel_detector_value(original, original.images[0],
                        channel.detector_settings, attribute, field)
                    if value is not None:
                        setattr(channel.detector_settings, attribute, value)
        for node in _walk(channel):
            if hasattr(node, "id") and node.id in instrument_maps[member_index]:
                node.id = instrument_maps[member_index][node.id]
        channel.id = f"Channel:0:{global_c}"
        old_refs, channel.annotation_refs = channel.annotation_refs, []
        source_annotations = {} if original.structured_annotations is None else {
            a.id: (field, a) for field in StructuredAnnotations.model_fields
            for a in getattr(original.structured_annotations, field)}
        for ref in old_refs:
            entry = source_annotations.get(ref.id)
            if entry is None:
                raise ValueError("Collection member contains a missing channel annotation")
            field, annotation = entry
            clone = annotation.model_copy(deep=True)
            clone.id = (f"Annotation:ChannelMetadata:0:{global_c}"
                if clone.namespace == canonical.CHANNEL_METADATA_NAMESPACE
                else f"Annotation:AlignedMember:{member_index}:{global_c}:{len(channel.annotation_refs)}")
            getattr(output.structured_annotations, field).append(clone)
            channel.annotation_refs.append(AnnotationRef(id=clone.id))
        annotation = MapAnnotation(id=f"Annotation:AlignedChannel:{global_c}", namespace=SOURCE_CHANNEL_NAMESPACE,
            value={"imaging_cycle": str(mapping.imaging_cycle), "cycle_authority": "registered_source_collection",
                   "source_member": str(member_index), "source_channel_index": str(local),
                   "source_channel_id": original_id, "source_file_sha256": collection.members[member_index].file_sha256})
        output.structured_annotations.map_annotations.append(annotation)
        channel.annotation_refs.append(AnnotationRef(id=annotation.id))
        pixels.channels.append(channel)
        for original_plane in original.images[0].pixels.planes:
            if original_plane.the_c == local:
                plane = original_plane.model_copy(deep=True)
                plane.the_c = global_c
                pixels.planes.append(plane)
    _merge_acquisition(output, collection, omes)
    if registration is not None:
        _retain_registration_acquisition(output, authorities, omes)
    annotation = MapAnnotation(id="Annotation:AlignedSourceCollection:0", namespace=COLLECTION_NAMESPACE,
        value={"descriptor": json.dumps(collection.to_mapping(), sort_keys=True, separators=(",", ":")),
               "member_authorities": json.dumps([a.to_mapping() for a in authorities], sort_keys=True, separators=(",", ":"))})
    output.structured_annotations.map_annotations.append(annotation)
    image.annotation_refs.append(AnnotationRef(id=annotation.id))
    output = _retain_processing_and_components(output, collection, omes)
    if registration is not None:
        import numpy as np
        pixels = output.images[0].pixels
        pixels.size_x, pixels.size_y = registration.crop[2:]
        pixels.type, pixels.significant_bits = "float", None
        base_level = replace(base_level, size_x=pixels.size_x, size_y=pixels.size_y, dtype=np.dtype("float32"))
    output = canonical.canonicalize_ome(output)
    storage = [a.series[a.selection.source_series_index].storage_bits for a in authorities]
    storage_bits = None
    if registration is None and all(item is not None for item in storage) and len({item.bits for item in storage}) == 1:
        from .backends import SourceStorageBits
        storage_bits = SourceStorageBits(storage[0].bits, "aligned_source_members")
    return output, replace(base_level, index=0, size_c=len(collection.channels), dimension_order="XYCZT"), storage_bits
