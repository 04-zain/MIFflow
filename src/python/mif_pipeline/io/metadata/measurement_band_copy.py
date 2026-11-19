"""Reviewed one-to-one component assignments for isolated measurement bands.

The researcher establishes signal isolation. Acquisition metadata supplies channel
identities and any recorded cycles; it cannot establish absence of spectral mixing.
"""
from __future__ import annotations

import json

from ome_types.model import AnnotationRef, MapAnnotation

from .canonical import (
    BiomarkerAssignment, ComponentIdentityMode, apply_component_identity_inventory,
    canonicalize_ome, resolve_channel_metadata, resolve_component_identity_inventory,
)
from .cycles import resolve_channel_cycles, split_cycle_component
from ..backends.errors import MetadataCanonicalizationError, MetadataConflictError


def _reviewed_markers(ome, inventory):
    if inventory.component_biomarkers is not None:
        return inventory.component_biomarkers
    if inventory.mode is ComponentIdentityMode.CHANNEL_FLUOROPHORES:
        rows = resolve_channel_metadata(ome, 0)
        if all(row.biomarker_assignment != "missing" for row in rows):
            return tuple(row.biomarker for row in rows)
    return None


def measurement_band_mapping_context(ome):
    """Offer existing reviewed rows; prefill only channel-bound metadata matches."""
    inventory = resolve_component_identity_inventory(ome, 0)
    if inventory is None:
        return {"bands": [], "components": []}
    cycles = resolve_channel_cycles(ome, 0)
    markers = _reviewed_markers(ome, inventory)
    components = [{"index": i, "name": name,
                   "cycle": (None if cycles is None else cycles[i])
                   if inventory.mode is ComponentIdentityMode.CHANNEL_FLUOROPHORES
                   else split_cycle_component(name)[1],
                   "marker": None if markers is None else markers[i]}
                  for i, name in enumerate(inventory.component_names)]
    bands = []
    for i, channel in enumerate(resolve_channel_metadata(ome, 0)):
        cycle = None if cycles is None else cycles[i]
        matches = []
        for j, name in enumerate(inventory.component_names):
            dye, component_cycle = split_cycle_component(name)
            if component_cycle != cycle and (component_cycle is not None or
                    (cycles is not None and len(set(cycles)) > 1)):
                continue
            if (channel.fluorophore is not None and channel.fluorophore == dye
                    and (channel.biomarker_assignment == "missing" or markers is None
                         or channel.biomarker == markers[j])):
                matches.append(j)
        selected = (i if inventory.mode is ComponentIdentityMode.CHANNEL_FLUOROPHORES
                    else matches[0] if len(matches) == 1 else None)
        bands.append({"channel_id": channel.channel_id, "name": channel.name or channel.channel_id,
                      "cycle": cycle, "component_index": selected})
    return {"bands": bands, "components": components}


def _copy_record(ome):
    from .spectral import PREPARED_CHANNELS_NAMESPACE, _strict_map
    linked = {ref.id for ref in ome.images[0].annotation_refs}
    records = [] if ome.structured_annotations is None else [a for a in
        ome.structured_annotations.map_annotations
        if a.id in linked and a.namespace == PREPARED_CHANNELS_NAMESPACE]
    if len(records) > 1:
        raise MetadataConflictError("Prepared-channel copy decision must be unique")
    return None if not records else _strict_map(records[0])


def validate_measurement_band_mapping(ome, mapping):
    """Require every real channel and every reviewed component exactly once."""
    inventory = resolve_component_identity_inventory(ome, 0)
    count = ome.images[0].pixels.size_c
    if (inventory is None or len(inventory.component_names) != count
            or type(mapping) is not tuple or len(mapping) != count
            or any(type(i) is not int for i in mapping) or set(mapping) != set(range(count))):
        raise MetadataCanonicalizationError(
            "Assign every measurement band to a different reviewed component; the numbers of bands and components must match"
        )
    record = _copy_record(ome)
    if record is not None:
        if (record.get("declaration_source") != "user_confirmed_measurement_band_isolation"
                or record.get("measurement_band_components") != json.dumps(mapping, separators=(",", ":"))):
            raise MetadataConflictError("The measurement-band mapping differs from the retained copy decision")
        try:
            original = json.loads(record["reviewed_component_order"])
            if (len(original) != count or tuple(original[i] for i in mapping) != inventory.component_names):
                raise ValueError("order differs")
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise MetadataConflictError("The retained mapping differs from the reviewed component order") from exc
        return inventory
    if _reviewed_markers(ome, inventory) is None:
        raise MetadataCanonicalizationError("Complete the component and marker review before assigning measurement bands")
    cycles = resolve_channel_cycles(ome, 0)
    for band, component in enumerate(mapping):
        name, component_cycle = split_cycle_component(inventory.component_names[component])
        source_cycle = None if cycles is None else cycles[band]
        if inventory.mode is ComponentIdentityMode.CHANNEL_FLUOROPHORES:
            component_cycle = None if cycles is None else cycles[component]
        if component_cycle != source_cycle and (component_cycle is not None or
                (cycles is not None and len(set(cycles)) > 1)):
            raise MetadataConflictError("The selected component cycle differs from its measurement band's recorded cycle")
        fluor = ome.images[0].pixels.channels[band].fluor
        if inventory.mode is ComponentIdentityMode.PLANNED_INVENTORY and fluor and fluor != name:
            raise MetadataConflictError("The selected component differs from the measurement band's recorded fluorophore")
    return inventory


def resolve_measurement_band_copy_mapping(ome):
    """Read a complete, channel-bound isolation decision from a prepared output."""
    from .spectral import resolve_prepared_channel_order
    record = _copy_record(ome)
    if record is None or record.get("declaration_source") != "user_confirmed_measurement_band_isolation":
        return None
    try:
        raw = json.loads(record["measurement_band_components"])
        if type(raw) is not list:
            raise ValueError("mapping is not a list")
        mapping = tuple(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise MetadataConflictError("The retained measurement-band mapping is invalid") from exc
    validate_measurement_band_mapping(ome, mapping)
    resolve_prepared_channel_order(ome, 0)
    return mapping


def prepare_measurement_band_copy(ome, mapping):
    """Attach the exact decision and apply its identities without altering pixels or history."""
    from .spectral import PREPARED_CHANNELS_NAMESPACE, _resolve_source_spectral_state, resolve_spectral_state
    from .canonical import resolve_unmixing_input_metadata
    state, _ = _resolve_source_spectral_state(ome, 0, spectral=resolve_spectral_state(ome, 0),
                                            acquisition=resolve_unmixing_input_metadata(ome, 0))
    if state == "unmixed":
        raise MetadataConflictError("This image records prior unmixing; choose the previously unmixed option")
    record = _copy_record(ome)
    if record is not None and record.get("declaration_source") == "user_confirmed_prepared_channels":
        # The channel-content review can already authorize generic preparation.
        # This explicit choice refines that decision without changing source history.
        ome = ome.model_copy(deep=True)
        removed = {a.id for a in ome.structured_annotations.map_annotations
                   if a.namespace == PREPARED_CHANNELS_NAMESPACE}
        ome.structured_annotations.map_annotations = [a for a in ome.structured_annotations.map_annotations
                                                       if a.id not in removed]
        ome.images[0].annotation_refs = [ref for ref in ome.images[0].annotation_refs if ref.id not in removed]
    inventory = validate_measurement_band_mapping(ome, mapping)
    if _copy_record(ome) is not None:
        return ome.model_copy(deep=True)
    names = tuple(inventory.component_names[i] for i in mapping)
    reviewed_markers = _reviewed_markers(ome, inventory)
    markers = tuple(reviewed_markers[i] for i in mapping)
    result = ome.model_copy(deep=True)
    identity_id = "Annotation:ComponentIdentity:0"
    result.images[0].annotation_refs = [ref for ref in result.images[0].annotation_refs if ref.id != identity_id]
    result.structured_annotations.map_annotations = [a for a in result.structured_annotations.map_annotations if a.id != identity_id]
    result = canonicalize_ome(result, assignments=tuple(
        BiomarkerAssignment(0, i, marker) for i, marker in enumerate(markers)))
    nuclear = inventory.nuclear_component_index
    result = apply_component_identity_inventory(result, 0, names,
        mode=ComponentIdentityMode.CHANNEL_FLUOROPHORES, promote_channel_fluorophores=False,
        decision_source="user_confirmed_measurement_band_isolation", component_biomarkers=markers,
        nuclear_component_index=None if nuclear is None else mapping.index(nuclear),
        nuclear_stain_reviewed=inventory.nuclear_stain_reviewed)
    used = {a.id for a in result.structured_annotations.map_annotations}
    index = 0
    while f"Annotation:PreparedChannels:{index}" in used:
        index += 1
    annotation = MapAnnotation(id=f"Annotation:PreparedChannels:{index}", namespace=PREPARED_CHANNELS_NAMESPACE,
        value={"declaration_source": "user_confirmed_measurement_band_isolation",
               "channel_ids": json.dumps(inventory.channel_ids, separators=(",", ":")),
               "component_order": json.dumps(names, separators=(",", ":")),
               "reviewed_component_order": json.dumps(inventory.component_names, separators=(",", ":")),
               "measurement_band_components": json.dumps(mapping, separators=(",", ":"))})
    result.structured_annotations.map_annotations.append(annotation)
    result.images[0].annotation_refs.append(AnnotationRef(id=annotation.id))
    return result
