"""Explicit cycle identities for an already assembled, channel-stacked image.

Canonical cycle numbers are zero based; vendor adapters normalize only verified
source conventions. Component labels use a one-based suffix for
the existing editable panel and spectral-table contracts; the suffix is a cycle
declaration, never evidence about a dye that was absent from the source.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from ome_types.model import AnnotationRef, MapAnnotation, StructuredAnnotations

from mif_pipeline.io.backends.errors import MetadataConflictError

SOURCE_CHANNEL_NAMESPACE = "urn:mif-pipeline:source-channel:v1"
CHANNEL_CYCLE_REVIEW_NAMESPACE = "urn:mif-pipeline:channel-cycle-review:v1"
_COMPONENT = re.compile(r"(.+) \[cycle ([1-9][0-9]*)\]", re.ASCII)


def split_cycle_component(name: str) -> tuple[str, int | None]:
    """Read only the explicit canonical cycle suffix, retaining the literal name."""
    match = _COMPONENT.fullmatch(name)
    return (match[1], int(match[2]) - 1) if match else (name, None)


def cycle_component_name(name: str, cycle: int) -> str:
    if type(cycle) is not int or cycle < 0:
        raise ValueError("An imaging cycle must be a zero-based nonnegative integer")
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise ValueError("A cycle component needs a nonempty component or dye name")
    base, recorded = split_cycle_component(name)
    if recorded is not None and recorded != cycle:
        raise ValueError("The component label declares a different imaging cycle")
    return f"{base} [cycle {cycle + 1}]"


def resolve_channel_cycles(ome, image_index=0, *, require_complete=False):
    """Read channel-bound cycle facts, without using names or acquisition order.

    Return None when no cycle is recorded. A partial tuple can populate review;
    numerical unmixing requests complete coverage before reading any pixels.
    """
    reviewed = resolve_channel_cycle_review(ome, image_index)
    cycles = reviewed if reviewed is not None else resolve_source_channel_cycles(ome, image_index)
    if cycles is not None and require_complete and any(cycle is None for cycle in cycles):
        raise MetadataConflictError(
            "Record an imaging cycle for every acquired band before cyclic unmixing; "
            "the current channel cycle mapping is incomplete."
        )
    return cycles


def resolve_source_channel_cycles(ome, image_index=0):
    """Read only normalized source facts; reviewed overrides remain separate."""
    annotations = {} if ome.structured_annotations is None else {
        annotation.id: annotation for annotation in ome.structured_annotations.map_annotations
    }
    cycles = []
    for channel in ome.images[image_index].pixels.channels:
        values = set()
        for ref in channel.annotation_refs:
            annotation = annotations.get(ref.id)
            if annotation is None or annotation.namespace != SOURCE_CHANNEL_NAMESPACE:
                continue
            entries = [entry.value for entry in annotation.value.ms if entry.k == "imaging_cycle"]
            if len(entries) > 1:
                raise MetadataConflictError(f"Channel {channel.id} has repeated imaging cycle metadata")
            for value in entries:
                if not value.isascii() or not value.isdecimal():
                    raise MetadataConflictError(f"Channel {channel.id} has an invalid imaging cycle")
                values.add(int(value))
        if len(values) > 1:
            raise MetadataConflictError(f"Channel {channel.id} has conflicting imaging cycles")
        cycles.append(next(iter(values), None))
    if not any(cycle is not None for cycle in cycles):
        return None
    return tuple(cycles)


def validate_channel_cycle_values(channel_ids, values):
    """Require an explicit nullable cycle in unchanged source channel order."""
    if not isinstance(values, (list, tuple)) or len(values) != len(channel_ids):
        raise ValueError("Cycle review must include every acquired channel in source order")
    numbers = []
    for channel_id, row in zip(channel_ids, values, strict=True):
        if (not isinstance(row, Mapping) or set(row) != {"channel_id", "cycle"}
                or row["channel_id"] != channel_id):
            raise ValueError("Cycle review must preserve the acquired channel IDs and source order")
        cycle = row["cycle"]
        if cycle is not None and (type(cycle) is not int or cycle < 0):
            raise ValueError("An imaging cycle must be a zero-based nonnegative integer or unknown")
        numbers.append(cycle)
    return tuple(numbers)


def resolve_channel_cycle_review(ome, image_index=0):
    """Read the accepted channel-bound review, including explicit unknown cycles."""
    annotations = {} if ome.structured_annotations is None else {
        annotation.id: annotation for annotation in ome.structured_annotations.map_annotations}
    values, present = [], []
    for channel in ome.images[image_index].pixels.channels:
        reviews = [annotations[ref.id] for ref in channel.annotation_refs
                   if ref.id in annotations and annotations[ref.id].namespace == CHANNEL_CYCLE_REVIEW_NAMESPACE]
        if len(reviews) > 1:
            raise MetadataConflictError(f"Channel {channel.id} has multiple accepted cycle reviews")
        present.append(bool(reviews))
        if not reviews:
            values.append(None)
            continue
        entries = reviews[0].value.ms
        fields = {entry.k: entry.value for entry in entries}
        if (len(entries) != 3 or set(fields) != {"channel_id", "imaging_cycle", "decision_source"}
                or fields["channel_id"] != channel.id or fields["decision_source"] != "researcher_review"):
            raise MetadataConflictError("Accepted cycle review differs from the acquired channel identity")
        number = fields["imaging_cycle"]
        if number != "unknown" and (not number.isascii() or not number.isdecimal()):
            raise MetadataConflictError("Accepted cycle review has an invalid imaging cycle")
        values.append(None if number == "unknown" else int(number))
    if not any(present):
        return None
    if not all(present):
        raise MetadataConflictError("Accepted cycle review must cover every acquired channel")
    return tuple(values)


def apply_channel_cycle_review(ome, values, image_index=0):
    """Copy an accepted cycle mapping without modifying the source cycle facts."""
    channels = ome.images[image_index].pixels.channels
    numbers = validate_channel_cycle_values(tuple(c.id for c in channels), values)
    result = ome.model_copy(deep=True)
    if resolve_channel_cycle_review(ome, image_index) == numbers:
        return result
    if result.structured_annotations is None:
        result.structured_annotations = StructuredAnnotations()
    annotations = result.structured_annotations.map_annotations
    channels = result.images[image_index].pixels.channels
    refs = {ref.id for channel in channels for ref in channel.annotation_refs}
    removed = {a.id for a in annotations if a.id in refs and a.namespace == CHANNEL_CYCLE_REVIEW_NAMESPACE}
    result.structured_annotations.map_annotations = [a for a in annotations if a.id not in removed]
    for channel, number in zip(channels, numbers, strict=True):
        channel.annotation_refs = [ref for ref in channel.annotation_refs if ref.id not in removed]
        annotation = MapAnnotation(id=f"Annotation:ChannelCycleReview:{channel.id}",
            namespace=CHANNEL_CYCLE_REVIEW_NAMESPACE,
            value={"channel_id": channel.id, "imaging_cycle": "unknown" if number is None else str(number),
                   "decision_source": "researcher_review"})
        if any(a.id == annotation.id for a in result.structured_annotations.map_annotations):
            raise MetadataConflictError("Accepted cycle review annotation ID is already in use")
        result.structured_annotations.map_annotations.append(annotation)
        channel.annotation_refs.append(AnnotationRef(id=annotation.id))
    return result


def replay_channel_cycle_review(source, reviewed):
    """Replay only the explicit accepted mapping when validating source changes."""
    result = source
    for index, image in enumerate(reviewed.images):
        numbers = resolve_channel_cycle_review(reviewed, index)
        if numbers is not None:
            result = apply_channel_cycle_review(result,
                [dict(channel_id=c.id, cycle=cycle) for c, cycle in zip(image.pixels.channels, numbers, strict=True)], index)
    return result


def apply_output_cycles(ome, cycles):
    """Attach the computed component's cycle without inventing source IFD facts."""
    if ome.structured_annotations is None:
        ome.structured_annotations = StructuredAnnotations()
    for index, cycle in enumerate(cycles):
        annotation = MapAnnotation(
            id=f"Annotation:UnmixedCycle:{index}", namespace=SOURCE_CHANNEL_NAMESPACE,
            value={"imaging_cycle": str(cycle), "cycle_authority": "accepted_cycle_reference"},
        )
        ome.structured_annotations.map_annotations.append(annotation)
        ome.images[0].pixels.channels[index].annotation_refs.append(AnnotationRef(id=annotation.id))
    return ome
