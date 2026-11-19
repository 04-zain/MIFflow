"""Turn inspected image choices into one analysis-owned sample source."""
from dataclasses import replace
import hashlib
import json
import re

from mif_pipeline.io import SourceSeriesInventory, bind_source_series_selection
from mif_pipeline.io.aligned_collection import (
    AlignedSourceChannel, AlignedSourceCollection, AlignedSourceMember,
    write_aligned_source_collection,
    load_aligned_source_collection,
)


def collection_from_samples(draft, rows, name, cycles, inventories, *, registration=None):
    """Validate selected original files without publishing a source descriptor."""
    rows = tuple(rows)
    if (len(rows) < 2 or len(set(rows)) != len(rows)
            or any(type(row) is not int or not 0 <= row < len(draft.samples) for row in rows)):
        raise ValueError("Select at least two inspected image rows to group")
    if type(name) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name) is None:
        raise ValueError("Use a sample name beginning with a letter or number, with letters, numbers, hyphens or underscores")
    if len(cycles) != len(rows) or len(inventories) != len(rows):
        raise ValueError("Every selected image needs its inspected channel count and imaging cycle")
    samples = tuple(draft.samples[row] for row in rows)
    for sample in samples:
        if sample.moving_reference_path is not None or sample.moving_reference_modality is not None:
            raise ValueError("Group the fluorescence files before assigning a paired image")
        if (sample.subject_id, sample.validation_domain_id, sample.overrides, sample.enabled) != (
                samples[0].subject_id, samples[0].validation_domain_id, samples[0].overrides, samples[0].enabled):
            raise ValueError("Grouped files must have matching subject, processing group and analysis settings")
    if any(sample.sample_id == name for row, sample in enumerate(draft.samples) if row not in rows):
        raise ValueError("This sample name is already in use")
    members = []
    for sample, inventory in zip(samples, inventories, strict=True):
        if (not isinstance(inventory, SourceSeriesInventory)
                or inventory.source_identity.resolved_path != sample.source_mif_path
                or inventory.source_identity.file_sha256 != sample.source_file_sha256):
            raise ValueError("Inspect every selected source image again before grouping")
        candidate = next((item for item in inventory.candidates
            if item.source_image_index == sample.source_image_index
            and item.source_series_index == sample.source_series_index), None)
        if candidate is None:
            raise ValueError("A selected image is missing from its inspected source inventory")
        bind_source_series_selection(inventory, candidate_id=candidate.candidate_id)
        members.append(AlignedSourceMember(sample.source_mif_path, sample.source_image_index,
            sample.source_series_index, sample.source_file_sha256, candidate.size_c, inventory.source_identity))
    channels = tuple(AlignedSourceChannel(i, channel, cycle)
        for i, (member, cycle) in enumerate(zip(members, cycles, strict=True))
        for channel in range(member.channel_count))
    return AlignedSourceCollection(name, tuple(members), channels, registration)


def group_aligned_samples(draft, rows, name, cycles, inventories, *, registration=None):
    """Publish an aligned declaration or explicitly reviewed rigid-cycle recipe."""
    rows = tuple(rows)
    collection = collection_from_samples(draft, rows, name, cycles, inventories, registration=registration)
    samples = tuple(draft.samples[row] for row in rows)
    digest = hashlib.sha256(json.dumps(collection.to_mapping(), sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    path = write_aligned_source_collection(draft.output_root / draft.batch_id /
        "source_collections" / f"{digest}.mif-source.json", collection)
    sample = replace(samples[0], sample_id=name, slide_id=name, source_mif_path=path,
        source_image_index=None, source_series_index=None, source_file_sha256=None)
    first = min(rows)
    updated = tuple(sample if row == first else existing
        for row, existing in enumerate(draft.samples) if row == first or row not in rows)
    return replace(draft, samples=updated), first


def ungroup_aligned_sample(draft, row):
    """Recover the retained member rows even when an original file is missing."""
    if type(row) is not int or not 0 <= row < len(draft.samples):
        raise ValueError("Select one grouped sample to restore its original files")
    sample = draft.samples[row]
    if sample.moving_reference_path is not None or sample.moving_reference_modality is not None:
        raise ValueError("Remove the paired-image assignment before ungrouping this sample")
    collection = load_aligned_source_collection(sample.source_mif_path)
    used = {item.sample_id for index, item in enumerate(draft.samples) if index != row}
    restored = []
    for index, member in enumerate(collection.members):
        base = sample.sample_id[:60] + f"-file-{index + 1}"
        name, suffix = base, 2
        while name in used:
            name = f"{base}-{suffix}"
            suffix += 1
        used.add(name)
        restored.append(replace(sample, sample_id=name, slide_id=name, source_mif_path=member.path,
            source_image_index=member.image_index, source_series_index=member.series_index,
            source_file_sha256=member.file_sha256))
    return replace(draft, samples=(*draft.samples[:row], *restored, *draft.samples[row + 1:]))
