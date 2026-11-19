"""Project the shared stain choice from the current prepared image into setup."""
from mif_pipeline.io.metadata.canonical import resolve_component_identity_inventory, resolve_channel_metadata


def saved_nuclear_channel_id(snapshot, *, sample_id, frame_ref, channels, prepared_ome=None):
    """Return the saved DNA-stain channel ID for this prepared image, or None.

    Only the selected image's root OME metadata is read. The result supplies
    setup defaults; existing KRONOS and measurement records retain the consumed
    role without changing canonical channel records or model input order.
    """
    ome = prepared_ome if prepared_ome is not None else saved_prepared_ome(
        snapshot, sample_id=sample_id, frame_ref=frame_ref)
    if ome is None:
        return None
    inventory = resolve_component_identity_inventory(ome, 0)
    if inventory is None or inventory.nuclear_component_index is None:
        return None
    observed = resolve_channel_metadata(ome, 0)
    if tuple(row.channel_id for row in observed) != tuple(row.channel_id for row in channels):
        raise ValueError("Channels and markers differs from the prepared image")
    index = inventory.nuclear_component_index
    if not 0 <= index < len(channels) or channels[index].biomarker is not None:
        raise ValueError("Review the nuclear stain in Channels and markers")
    return channels[index].channel_id


def saved_prepared_ome(snapshot, *, sample_id, frame_ref):
    """Read the accepted prepared image's small root metadata, without pixels."""
    from .batch_analysis_preflight import validate_analysis_frame_artifact_ref
    frame = validate_analysis_frame_artifact_ref(frame_ref)
    matches = [ref for ref in snapshot.accepted_artifacts
        if ref.artifact_kind == "processed_image" and ref.scope.sample_id == sample_id
        and ref.sha256 == frame.spatial_frame.canonical_image_sha256]
    if not matches:
        return None
    if len(matches) != 1 or matches[0].locator is None:
        raise ValueError("The prepared image for Channels and markers is unavailable")
    path = matches[0].locator
    import json
    from ome_types import from_xml
    processing = json.loads((path / "zarr.json").read_text())["attributes"]["mif_pipeline"]
    if processing.get("schema") != "mif-processed-image/v1" or processing.get("complete") is not True:
        raise ValueError("Channels and markers requires a completed prepared image")
    return from_xml(processing["ome_xml"])


def kronos_intensity_divisor(ome):
    """Suggest the pipeline's digital-range convention from saved preparation.

    Full correction already includes digital-range division. Exposure-only
    correction does not. These are acquisition-rate conventions, not a claim
    that the resulting values match KRONOS pretraining or lie within zero to one.
    """
    if ome is None:
        return None
    from mif_pipeline.io.metadata.spectral import resolve_processing_provenance
    from mif_pipeline.io.backends import MetadataCanonicalizationError
    try:
        provenance = resolve_processing_provenance(ome)
    except (MetadataCanonicalizationError, ValueError):
        # This optional suggestion must not prevent manual intensity scaling.
        return None
    policy = provenance.normalization_policy
    if policy == "mif_pipeline_offset_exposure_v1":
        return 1.0
    if policy == "mif_pipeline_exposure_only_v1":
        if provenance.effective_digital_range is not None:
            return provenance.effective_digital_range.maximum
        if provenance.ome_significant_bits is not None:
            return float(2 ** provenance.ome_significant_bits - 1)
        return {"uint8": 255.0, "uint16": 65535.0}.get(provenance.source_storage_dtype)
    if policy == "not_applied" and provenance.source_intensity_representation != "normalized_counts":
        # Copied unsigned samples retain the official storage-range convention.
        return {"uint8": 255.0, "uint16": 65535.0}.get(provenance.source_storage_dtype)
    return None


def shared_kronos_intensity_divisor(values):
    """Offer one value only when every participating image has the same value."""
    values = tuple(values)
    return values[0] if values and values[0] is not None and all(
        value == values[0] for value in values) else None
