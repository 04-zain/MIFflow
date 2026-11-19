"""

Collects imaging settings and prepares the review of values missing from an image.

Quantitative unmixing needs exposure, gain, binning
and detector offsets for each band. Detected values are retained with their channel
positions. Missing cells are identified by field and channel number, for example
"gain:1" for the second channel.

The researcher supplies those missing values together with their source and reviewer.
Completion creates an updated OME copy and records the original acquisition checksum.
An incomplete acquisition can instead use explicitly selected exposure-only correction;
its unused missing settings remain absent. The checksum and selected mode allow the
same review to be applied when the same image is reopened.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from dataclasses import replace
from collections.abc import Mapping
import hashlib, json, math
import numpy as np
from ome_types import OME
from . import canonical as c

PENDING_NS = "urn:mif-pipeline:acquisition-inventory:v1"
REVIEW_NS = "urn:mif-pipeline:calibration-review:v1"
FIELD_INFO = {
    "exposure_seconds": ("Exposure time", "seconds", "number"),
    "gain": ("Detector gain", "multiplier", "number"),
    "binning": ("Detector binning", "pixels per side", "integer"),
    "offsets": ("Detector offset", "stored pixel intensity units", "number"),
}

# ==============================================================================
#                                  Calibration
# ==============================================================================


def _json(value):
    """

    Writes a small record as compact JSON with sorted keys for consistent storage and checksums.

    Args:
        value: JSON-serializable record containing finite values.

    Returns:
        The encoded JSON text.
    """

    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    """

    Calculates the checksum used to recognize an acquisition or review record on reopening.

    Args:
        value: Record to encode using the shared sorted-key JSON format.

    Returns:
        Its SHA-256 checksum.
    """

    return hashlib.sha256(_json(value).encode()).hexdigest()


def _find(ome, index, namespace):
    """

    Finds the acquisition or calibration-review record linked to one OME image.

    Follow the image's annotation references and select the requested namespace, then
    decode the JSON stored under "record". Multiple matching records are rejected
    because the image must have one acquisition/review description for that namespace.

    Args:
        ome: Metadata containing images and structured annotations.
        index: Image position whose links should be followed.
        namespace: Acquisition or review record type to find.

    Returns:
        The decoded record, or None when no matching annotation is linked.

    Raises:
        MetadataConflictError: Matching records or map fields are duplicated.
        KeyError, ValueError: The record field is missing or its JSON is invalid.
    """

    if ome.structured_annotations is None:
        return None
    refs = {r.id for r in ome.images[index].annotation_refs}
    matches = [
        a
        for a in ome.structured_annotations.map_annotations
        if a.id in refs and a.namespace == namespace
    ]
    if len(matches) > 1:
        raise c.MetadataConflictError("Duplicate imaging-settings review metadata")
    return None if not matches else json.loads(c._annotation_value(matches[0])["record"])


def _put(ome, index, namespace, name, value):
    """

    Stores a JSON record in an image-linked OME MapAnnotation.

    Build its ID from the record name and image position, add or reuse the matching
    annotation, then ensure that the image links to it once. An existing ID with
    different content is reported as a conflicting record.

    Args:
        ome: Working metadata copy to update.
        index: Image position receiving the record.
        namespace: Name identifying the record format.
        name: Annotation ID component, such as "CalibrationReview".
        value: Record to serialize under the annotation's "record" field.

    Raises:
        MetadataConflictError: The intended ID already contains different information.
    """

    annotation = c._ensure_annotation(
        ome,
        annotation_id=f"Annotation:{name}:{index}",
        namespace=namespace,
        value={"record": _json(value)},
    )
    c._link_once(ome.images[index].annotation_refs, annotation.id)


def _numeric(value, field):
    """

    Checks an entered acquisition value according to the field it completes.

    Exposure, gain and binning must be positive; detector offsets can
    also be zero or negative. Binning requires a whole number.

    Args:
        value: Detected or manually supplied field value.
        field: Acquisition field name from FIELD_INFO.

    Returns:
        The checked integer or float required by that field.

    Raises:
        ValueError: The type, numerical range or whole-number requirement is invalid.
    """

    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    if field != "offsets" and value <= 0:
        raise ValueError(f"{field} must be positive")
    if field == "binning":
        if int(value) != value:
            raise ValueError(f"{field} must be a whole number")
        return int(value)
    return float(value)


def read_original_metadata(
    ome: OME, requested: set[str], *, include_numbered: bool = False
) -> dict[str, str]:
    """Read the requested vendor fields from OME OriginalMetadata annotations.

    We collect only the keys needed by the caller, optionally including their
    Bio-Formats " #N" repetitions. Unused keys are neither indexed nor checked
    for duplicate values. The caller establishes which vendor
    and image the values describe before interpreting their meaning or units.

    Args:
        ome: OME metadata produced during source inspection.
        requested: Exact OriginalMetadata keys required for the current decision.
        include_numbered: Include each requested key's numeric " #N" suffixes,
            used for repeated LSM recordings with a proven image-name prefix.

    Returns:
        Literal strings for the requested fields that are present.

    Raises:
        MetadataCanonicalizationError: A requested field is duplicated or ambiguous.
    """
    result = {}
    if not requested or ome.structured_annotations is None:
        return result
    for annotation in ome.structured_annotations.xml_annotations:
        if annotation.namespace != c.ORIGINAL_METADATA_NAMESPACE:
            continue
        pending = list(annotation.value.any_elements)
        keys, values = [], []
        while pending:
            element = pending.pop()
            name = str(getattr(element, "qname", "")).rsplit("}", 1)[-1].rsplit(":", 1)[-1]
            if name == "Key":
                keys.append(getattr(element, "text", None) or "")
            elif name == "Value":
                values.append(getattr(element, "text", None) or "")
            pending.extend(getattr(element, "children", ()))
        if not any(
            key in requested or (
                include_numbered and any(
                    key.startswith(name + " #") and key[len(name) + 2:].isascii()
                    and key[len(name) + 2:].isdigit()
                    for name in requested
                )
            )
            for key in keys
        ):
            continue
        if len(keys) != 1 or len(values) != 1:
            raise c.MetadataCanonicalizationError(
                f"Ambiguous requested field in OriginalMetadata {annotation.id}"
            )
        key = keys[0]
        if key in result:
            raise c.MetadataCanonicalizationError(f"Duplicate OriginalMetadata field: {key!r}")
        result[key] = values[0]
    return result


def _channel_detector_value(ome, image, settings, attribute, field):
    """Resolve a numerical detector setting before its linked fixed detector value.

    We use the channel's setting when present. Otherwise the image InstrumentRef
    and settings detector ID must identify one fixed detector. Missing references
    leave the value unavailable; duplicate matching IDs reject the needed fallback.
    Present values are validated even when a lower-priority source is available.
    """
    value = getattr(settings, attribute)
    if value is not None:
        return _numeric(float(value), field)
    if image.instrument_ref is None:
        return None
    instruments = [item for item in ome.instruments if item.id == image.instrument_ref.id]
    if len(instruments) > 1:
        raise c.MetadataConflictError("Image instrument reference is ambiguous")
    if not instruments:
        return None
    detectors = [item for item in instruments[0].detectors if item.id == settings.id]
    if len(detectors) > 1:
        raise c.MetadataConflictError("Channel detector reference is ambiguous")
    value = getattr(detectors[0], attribute) if detectors else None
    return None if value is None else _numeric(float(value), field)


def _source_channel_autofluorescence(ome, index):
    """Return positional vendor facts, including missing per-channel flags."""
    from .cycles import SOURCE_CHANNEL_NAMESPACE
    annotations = {} if ome.structured_annotations is None else {
        annotation.id: annotation for annotation in ome.structured_annotations.map_annotations}
    flags = []
    channels = ome.images[index].pixels.channels
    for channel in channels:
        records = [annotations[reference.id] for reference in channel.annotation_refs
            if reference.id in annotations and annotations[reference.id].namespace == SOURCE_CHANNEL_NAMESPACE
            and c._annotation_value(annotations[reference.id]).get('source_reader') == 'loci.formats.in.VectraReader']
        if not records:
            return None
        if len(records) != 1:
            raise c.MetadataConflictError(f'Channel {channel.name or channel.id} has multiple source processing inventories')
        values = [entry.value for entry in records[0].value.ms if entry.k == 'autofluorescence_subtracted']
        if len(values) > 1:
            raise c.MetadataConflictError(f'Channel {channel.name or channel.id} has repeated autofluorescence declarations')
        flags.append(None if not values else c._parse_bool(values[0], 'AutofluorescenceSubtracted'))
    if len({flag for flag in flags if flag is not None}) > 1:
        details = '; '.join(f'{channel.name or channel.id} (C{i}): {flag}'
            for i, (channel, flag) in enumerate(zip(channels, flags)) if flag is not None)
        raise c.MetadataConflictError('Source channels have different recorded autofluorescence subtraction states: '
            + details + '. Export channels with a consistent processing state before spectral fitting.')
    return tuple(flags)


def _extract(ome, index, *, vectra_source=False, source_acquisition_values=None):
    """Collect typed imaging settings, then fill only missing vendor values.

    Channel DetectorSettings precede the explicitly linked fixed Detector for
    gain and offset. Typed OME quantities convert exposure units to seconds, and typed binning
    supplies a square side. Supported OriginalMetadata fields can fill missing
    cells only when their image and channel association is unambiguous.

    Existing linked processing state precedes Vectra state annotations. Only a
    proven Vectra analytical image can use its OriginalMetadata. Its SignalUnits
    low nibble identifies raw, normalized, dark-corrected or optical-density
    values; the weighting nibble never selects an intensity scale. Missing offsets
    first use Offset, then explicitly zero OffsetCounts. Nonzero camera counts
    leave the baseline missing for review. Unused vendor fields and overridden
    vector cells are not interpreted. Zero exposure is unavailable only for an
    explicitly unmixed image. ADC depth remains optional retained provenance.

    Args:
        ome: Source metadata with typed imaging settings and retained annotations.
        index: Image position whose channels define every per-band vector.
        vectra_source: True only for image zero after live reader/topology proof.
        source_acquisition_values: Optional exposure_seconds and binning vectors
            attributed to this image by the live reader, used only in missing cells.

    Returns:
        Channel identities, pixel range, processing state and per-field lists.
        Values still unavailable remain None for explicit acquisition review.

    Raises:
        MetadataConflictError: Required references or channel coverage are ambiguous.
        MetadataCanonicalizationError: A needed vendor field cannot be parsed.
        ValueError: A used acquisition value fails its numerical requirements.
    """
    from .spectral import resolve_spectral_state

    image = ome.images[index]
    pixels = image.pixels
    count = len(pixels.channels)
    if vectra_source and index != 0:
        raise c.MetadataConflictError("Vectra imaging settings belong to source image zero")
    spectral = resolve_spectral_state(ome, index)
    pixel_type = getattr(pixels.type, "value", pixels.type)
    dtype = np.dtype({"float": "float32", "double": "float64"}.get(pixel_type, pixel_type))
    if dtype.kind not in "iuf":
        raise c.MetadataConflictError("Imaging-settings review requires a real numeric pixel type")
    data = {
        "channel_ids": [ch.id for ch in pixels.channels],
        "band_names": [ch.name or ch.id for ch in pixels.channels],
        "dtype": dtype.name,
        "storage_bits": dtype.itemsize * 8,
        "storage_max": float(np.iinfo(dtype).max) if dtype.kind in "iu" else None,
        "spectral_state": spectral.state,
        "autofluorescence_subtracted": spectral.autofluorescence_subtracted,
        "values": {field: [None] * count for field in FIELD_INFO},
        "offset_authority": c.OffsetAuthority.METADATA.value,
        "intensity_representation": "unknown",
    }
    data["values"]["camera_bit_depth"] = []
    from .cycles import resolve_channel_cycles, cycle_component_name
    cycles = resolve_channel_cycles(ome, index)
    if cycles is not None and len({cycle for cycle in cycles if cycle is not None}) > 1:
        data["band_names"] = [
            cycle_component_name(name, cycle) if cycle is not None else name
            for name, cycle in zip(data["band_names"], cycles, strict=True)
        ]
    for i, channel in enumerate(pixels.channels):
        detector = channel.detector_settings
        if detector is None:
            continue
        for attribute, field in (("gain", "gain"), ("offset", "offsets")):
            data["values"][field][i] = _channel_detector_value(
                ome, image, detector, attribute, field
            )
        if detector.binning is not None:
            value = str(getattr(detector.binning, "value", detector.binning)).lower()
            if value == "other":
                continue
            binning = value.split("x")
            if len(binning) != 2 or binning[0] != binning[1]:
                raise c.MetadataConflictError("Unsupported non-square detector binning")
            data["values"]["binning"][i] = _numeric(int(binning[0]), "binning")

    channel_af = None
    if data["autofluorescence_subtracted"] is None:
        channel_af = _source_channel_autofluorescence(ome, index)
        if channel_af is not None and channel_af and all(flag is not None for flag in channel_af):
            data["autofluorescence_subtracted"] = channel_af[0]
    requested = set()
    if vectra_source:
        requested.add("SignalUnits")
        if any(value is None for value in data["values"]["offsets"]):
            requested.add("Offset")
        if data["spectral_state"] == "unknown":
            requested.add("IsUnmixedComponent")
        if data["autofluorescence_subtracted"] is None and channel_af is None:
            requested.add("AutofluorescenceSubtracted")
    original = read_original_metadata(ome, requested) if requested else {}
    if "SignalUnits" in original:
        codes = c._literal_vector(original["SignalUnits"], "SignalUnits")
        if len(codes) != count or any(type(code) is not int or code < 0 or code > 255 for code in codes):
            raise c.MetadataConflictError("SignalUnits must describe each channel with a byte code")
        units = {code & 15 for code in codes}
        if len(units) != 1:
            raise c.MetadataConflictError("Channels have different stored intensity representations")
        data["intensity_representation"] = {
            0: "raw_counts", 1: "normalized_counts", 4: "optical_density", 5: "dark_corrected_counts"
        }.get(next(iter(units)), "unsupported")
    if "Offset" in original:
        offsets = c._literal_vector(original["Offset"], "Offset")
        if len(offsets) != count:
            raise c.MetadataConflictError("Offset does not match the recorded channels")
        for i, value in enumerate(offsets):
            if data["values"]["offsets"][i] is None and value is not None:
                data["values"]["offsets"][i] = _numeric(value, "offsets")
    if vectra_source and any(value is None for value in data["values"]["offsets"]):
        counts_metadata = read_original_metadata(ome, {"OffsetCounts"})
        if "OffsetCounts" in counts_metadata:
            counts = c._literal_vector(counts_metadata["OffsetCounts"], "OffsetCounts")
            if len(counts) != count:
                raise c.MetadataConflictError("OffsetCounts does not match the recorded channels")
            for i, value in enumerate(counts):
                if data["values"]["offsets"][i] is None and value is not None:
                    if _numeric(value, "offsets") == 0:
                        data["values"]["offsets"][i] = 0.0
    if "IsUnmixedComponent" in original:
        flags = c._boolean_vector(original["IsUnmixedComponent"], "IsUnmixedComponent", count)
        if len(set(flags)) != 1:
            raise c.MetadataConflictError("Recorded spectral states disagree")
        # QPTIFF defines True as an unmixed multispectral component. False
        # also covers separately acquired conventional/cyclic channels, so it
        # cannot establish that the channels are raw spectral measurements.
        if flags[0]:
            data["spectral_state"] = "unmixed"
    if "AutofluorescenceSubtracted" in original:
        flags = c._literal_vector(
            original["AutofluorescenceSubtracted"], "AutofluorescenceSubtracted"
        )
        values = [c._parse_bool(v, "AutofluorescenceSubtracted") for v in flags]
        if len(values) not in (1, count):
            raise c.MetadataConflictError(
                f"Autofluorescence metadata lists {len(values)} values for {count} source channels, "
                "without identifying the missing channel positions. Use an export that retains each channel's processing metadata.")
        if len(set(values)) != 1:
            details = '; '.join(f'{channel.name or channel.id} (C{i}): {flag}'
                for i, (channel, flag) in enumerate(zip(pixels.channels, values)))
            raise c.MetadataConflictError('Source channels have different recorded autofluorescence subtraction states: '
                + details + '. Export channels with a consistent processing state before spectral fitting.')
        data["autofluorescence_subtracted"] = values[0]

    seen_exposure_channels = set()
    for plane in pixels.planes:
        if plane.the_t or plane.the_z or plane.exposure_time is None:
            continue
        i = int(plane.the_c)
        if not 0 <= i < count or i in seen_exposure_channels:
            raise c.MetadataConflictError("Exposure planes are ambiguous")
        seen_exposure_channels.add(i)
        seconds = float(plane.exposure_time_quantity.to("second").magnitude)
        if seconds == 0 and data["spectral_state"] == "unmixed":
            continue
        data["values"]["exposure_seconds"][i] = _numeric(seconds, "exposure_seconds")
    if source_acquisition_values is not None:
        if not isinstance(source_acquisition_values, Mapping) or set(source_acquisition_values) - {
            "exposure_seconds", "binning"
        }:
            raise c.MetadataConflictError("Unsupported source imaging settings")
        for field, values in source_acquisition_values.items():
            if all(value is not None for value in data["values"][field]):
                continue
            if not isinstance(values, (tuple, list)) or len(values) != count:
                raise c.MetadataConflictError("Source per-band imaging-settings vector differs from image channels")
            for i, value in enumerate(values):
                if data["values"][field][i] is None and value is not None:
                    data["values"][field][i] = _numeric(value, field)
    return data


def retain_incomplete_acquisition(
    ome, index, *, vectra_source=False, source_acquisition_values=None
):
    """Retain the imaging-settings inventory for one explicitly selected source image.

    Existing linked inventories retain their values and review checksum. New values
    use the shared collector. Complete required fields install the same acquisition
    record used by reviewed completion; absent ADC provenance never blocks it.

    Args:
        ome: Working metadata to receive the image-linked inventory and completion.
        index: Explicit image position; channel count never selects an image.
        vectra_source: Whether the backend proved the Vectra source-image association.
        source_acquisition_values: Optional channel-ordered exposure_seconds and
            binning vectors established by the live source reader. These fill
            missing typed OME values in a new inventory. Present typed values
            take precedence. Existing retained inventories are reused.

    Returns:
        None; the working OME object receives any newly established records.

    Raises:
        MetadataConflictError: Retained channel identities or imaging settings conflict.
        MetadataCanonicalizationError: A requested field or annotation is invalid.
    """
    if c.resolve_unmixing_input_metadata(ome, index) is not None:
        return
    data = _find(ome, index, PENDING_NS)
    if data is None:
        data = _extract(
            ome, index, vectra_source=vectra_source,
            source_acquisition_values=source_acquisition_values,
        )
    elif data["channel_ids"] != [ch.id for ch in ome.images[index].pixels.channels]:
        raise c.MetadataConflictError("Retained imaging-settings inventory differs from image channels")
    _put(ome, index, PENDING_NS, "AcquisitionInventory", data)
    if data["intensity_representation"] == "normalized_counts":
        _install(ome, index, data, data["values"], data["spectral_state"], None, correction_mode="as_stored")
        return
    if data["intensity_representation"] in {"optical_density", "unsupported"}:
        return
    if data["storage_max"] is not None and not any(
        v is None for field in FIELD_INFO
        if field != "offsets" or data["intensity_representation"] != "dark_corrected_counts"
        for v in data["values"][field]
    ):
        _install(ome, index, data, data["values"], data["spectral_state"], None)



def retained_acquisition_state(ome, index=0):
    """Read spectral state from an image-linked inventory without parsing imaging settings.

    Args:
        ome: Metadata containing any retained imaging-settings inventory.
        index: Selected image position, defaulting to zero.

    Returns:
        The retained raw/unmixed/unknown state; absence remains unknown.

    Raises:
        MetadataConflictError: Inventory channel identities or its state are invalid.
    """
    data = _find(ome, index, PENDING_NS)
    if data is None:
        return "unknown"
    if data["channel_ids"] != [ch.id for ch in ome.images[index].pixels.channels]:
        raise c.MetadataConflictError("Retained imaging-settings inventory differs from image channels")
    state = data["spectral_state"]
    if state not in {"raw", "unmixed", "unknown"}:
        raise c.MetadataConflictError("Retained image spectral state is invalid")
    return state


def recorded_autofluorescence_subtracted(ome, index=0):
    """Read the AF-subtraction flag from the selected image's saved OME metadata.

    Completed image metadata supplies the flag when available. Otherwise,
    we use the retained imaging settings or their original OME annotations.
    Missing information remains unknown; the vendor image is never reopened.
    """
    complete = c.resolve_unmixing_input_metadata(ome, index)
    if complete is not None:
        return complete.autofluorescence_subtracted
    data = _find(ome, index, PENDING_NS) or _extract(ome, index)
    return data["autofluorescence_subtracted"]


def calibration_inventory(ome, index=0):
    """

    Builds the missing-field list shown during acquisition review.

    Each missing cell carries its field name, channel position, displayed label, unit
    and input type. Known values stay in the detected record. A checksum of that record
    connects the completed form to the imaging settings the researcher reviewed.

    Args:
        ome: Image metadata containing detected imaging settings.
        index: Image position, defaulting to zero.

    Returns:
        The channel list, missing-field rows and processing-state information.
        Already complete image metadata returns an empty missing list.
    """

    complete = c.resolve_unmixing_input_metadata(ome, index)
    if complete is not None:
        return {
            "schema_version": "missing-calibration/v1",
            "channel_ids": [ch.id for ch in ome.images[index].pixels.channels],
            "band_names": list(complete.band_order),
            "missing": [],
            "spectral_state": complete.spectral_state,
            "correction_mode": complete.correction_mode,
            "intensity_representation": complete.intensity_representation,
        }
    data = _find(ome, index, PENDING_NS) or _extract(ome, index)
    missing = []
    for field, (label, unit, kind) in FIELD_INFO.items():
        if field == "offsets" and data["intensity_representation"] == "dark_corrected_counts":
            continue  # Historical offset is not an input to a second subtraction.
        for i, value in enumerate(data["values"][field]):
            if value is None:
                missing.append(
                    {
                        "key": f"{field}:{i}",
                        "field": field,
                        "channel_id": data["channel_ids"][i],
                        "channel_index": i,
                        "channel_name": data["band_names"][i],
                        "label": label,
                        "units": unit,
                        "kind": kind,
                    }
                )
    return {
        "schema_version": "missing-calibration/v1",
        "channel_ids": data["channel_ids"],
        "band_names": data["band_names"],
        "missing": missing,
        "spectral_state": data["spectral_state"],
        "input_sha256": _digest(data),
        "intensity_representation": data["intensity_representation"],
    }


def raw_intensity_error(representation: str) -> str | None:
    """Explain an unsupported raw-input representation and how to replace it.

    Args:
        representation: Stored-intensity interpretation recorded for this image.

    Returns:
        An actionable error for optical-density or unsupported values;
        None when this representation does not prevent raw intensity correction.
    """
    cause = {
        "optical_density": "Optical-density values cannot be used for fluorescence unmixing.",
        "unsupported": "The recorded stored-intensity representation is unsupported for fluorescence unmixing.",
    }.get(representation)
    if cause is None:
        return None
    return (
        cause + " An original unnormalized fluorescence export is required. "
        "Calibration values cannot make this file suitable for raw fluorescence correction."
    )


def _install(ome, index, data, values, state, proof, *, correction_mode="full"):
    """

    Writes completed imaging settings into the image's standard unmixing annotation.

    Convert recorded square binning sides to areas and retain absent unused cells
    for exposure-only or as-stored processing. Keep all vectors in channel order
    and reject unsupported intensity representations. Metadata-known
    dark correction leaves recorded offsets intact; calculation derives zero.
    Manually completed offsets retain their reviewed profile and review checksum.

    Args:
        ome: Working metadata copy receiving the completed annotation.
        index: Position of the selected image.
        data: Detected channel, storage and processing-state information.
        values: Recorded per-band imaging settings; unused cells may be None.
        state: Raw/unmixed state recorded with the settings.
        proof: Review record for supplied values and correction choice, or None.
        correction_mode: Full, explicit exposure_only, or metadata-selected as_stored.
    """

    if correction_mode not in {"full", "exposure_only", "as_stored"}:
        raise ValueError("Unsupported intensity correction mode")
    if (correction_mode == "as_stored") != (data["intensity_representation"] == "normalized_counts"):
        raise c.MetadataConflictError("Metadata-known normalized intensities must be used as stored")
    representation = data["intensity_representation"]
    message = raw_intensity_error(representation)
    if state != "unmixed" and message is not None:
        raise c.MetadataConflictError(message)
    # We establish the camera-count range when installing raw acquisition correction.
    # Known normalized linear values bypass that range; component images use copy.
    if correction_mode != "as_stored" and np.dtype(data["dtype"]).kind not in "iu":
        raise c.MetadataConflictError("Raw calibration requires an integer stored-pixel range")
    offsets = tuple(values["offsets"])
    manual_offset = proof is not None and any(k.startswith("offsets:") for k in proof["values"])
    authority = (
        c.OffsetAuthority.EXPLICIT_VALIDATED_PROFILE
        if manual_offset
        else c.OffsetAuthority(data["offset_authority"])
    )
    metadata = c.UnmixingInputMetadata(
        image_index=index,
        spectral_state=state,
        state_source=(
            "reviewed_acquisition_completion" if proof else "canonicalized_acquisition_metadata"
        ),
        band_order=tuple(data["band_names"]),
        exposure_seconds=tuple(values["exposure_seconds"]),
        gain=tuple(values["gain"]),
        binning=tuple(values["binning"]),
        binning_area=tuple(None if v is None else v * v for v in values["binning"]),
        correction_mode=correction_mode,
        intensity_representation=representation,
        source_storage_dtype=data["dtype"],
        source_storage_bits=data["storage_bits"],
        source_storage_max=data["storage_max"],
        digital_range_profile_id=data.get("digital_range_profile_id"),
        digital_range_meaningful_bits=data.get("digital_range_meaningful_bits"),
        digital_range_maximum=data.get("digital_range_maximum"),
        camera_bit_depth=(
            tuple(values["camera_bit_depth"])
            if all(v is not None for v in values["camera_bit_depth"])
            else ()
        ),
        autofluorescence_subtracted=data["autofluorescence_subtracted"],
        offsets=offsets,
        offset_authority=authority,
        offset_profile_identifier=("user-calibration:" + _digest(proof)) if manual_offset else None,
    )
    c.validate_offset_specification(
        tuple(v for v in metadata.offsets if v is not None),
        metadata.offset_authority, metadata.offset_profile_identifier
    )
    annotation = c._ensure_annotation(
        ome,
        annotation_id=f"Annotation:UnmixingInput:{index}",
        namespace=c.UNMIXING_INPUT_NAMESPACE,
        value=c._encode_unmixing_input_metadata(metadata, channel_ids=tuple(data["channel_ids"])),
    )
    c._link_once(ome.images[index].annotation_refs, annotation.id)


def complete_calibration(
    ome, values, *, source_note, reviewer, spectral_state, index=0, correction_mode="full"
):
    """

    Fills the missing acquisition cells and records who supplied them and where they came from.

    Rebuild the missing-field list before applying the response. Full correction
    requires every missing cell; exposure-only requires only missing exposures and
    retains every other absent setting. Detected values cannot be overwritten.
    Save the selected mode, supplied entries, reviewer and original acquisition
    checksum in the OME copy. A source note is required when values are entered.
    Complete numeric metadata with an unknown spectral state also permits an empty
    value mapping to record raw-state confirmation without changing detected values.
    Its metadata-selected as-stored convention remains fixed when present.

    Args:
        ome: Metadata containing the detected settings.
        values: Missing field keys mapped to entered values, such as {"gain:0": 2.0}.
        source_note: Explanation of the acquisition record or measurement supplying the values.
        reviewer: Name or identifier of the person completing the review.
        spectral_state: "raw" or "unmixed", consistent with any known source state.
        index: Reviewed image position, defaulting to zero.
        correction_mode: Full manual completion or exposure_only correction under
            the displayed identical-acquisition-settings requirement.

    Returns:
        OME metadata containing the selected correction and acquisition review.

    Raises:
        ValueError: Keys, entries, review information or the chosen state are invalid.
        MetadataConflictError: The completion conflicts with an existing annotation.
    """

    inventory = calibration_inventory(ome, index)
    confirmed_stored = (
        correction_mode == "as_stored" and inventory.get("correction_mode") == "as_stored"
        and inventory["spectral_state"] == "unknown"
    )
    if correction_mode not in {"full", "exposure_only"} and not confirmed_stored:
        raise ValueError("Choose an intensity correction method")
    if not inventory["missing"] and inventory["spectral_state"] != "unknown":
        raise ValueError("Complete image metadata uses its recorded correction")
    missing = {
        row["key"]: row for row in inventory["missing"]
        if correction_mode == "full" or row["field"] == "exposure_seconds"
    }
    if not isinstance(values, Mapping) or set(values) != set(missing):
        raise ValueError(
            "Enter exactly the missing imaging settings; detected metadata cannot be changed here"
        )
    if spectral_state not in {"raw", "unmixed"} or inventory["spectral_state"] not in {
        "unknown",
        spectral_state,
    }:
        raise ValueError("The selected spectral state conflicts with the image")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("Record the reviewer of the intensity correction choice")
    if missing and (not isinstance(source_note, str) or not source_note.strip()):
        raise ValueError("Record the source of the supplied imaging settings")
    source_note = source_note or ""

    # Apply the completed values to a metadata copy after checking the missing-field list.
    result = ome.model_copy(deep=True)
    data = _find(ome, index, PENDING_NS) or _extract(ome, index)
    completed = json.loads(_json(data["values"]))
    entered = {}
    for key, row in missing.items():
        value = _numeric(values[key], row["field"])
        completed[row["field"]][row["channel_index"]] = value
        entered[key] = value

    # Record the entered cells with their source note, reviewer and original acquisition
    # checksum.
    proof = {
        "schema_version": "calibration-review/v1",
        "input_sha256": _digest(data),
        "values": entered,
        "source_note": source_note.strip(),
        "reviewer": reviewer.strip(),
        "spectral_state": spectral_state,
        "correction_mode": correction_mode,
    }
    if not inventory["missing"]:
        # Replace only our complete unknown-state record after explicit confirmation.
        # The detected numeric values above are unchanged and remain replayable.
        annotation_id = f"Annotation:UnmixingInput:{index}"
        result.structured_annotations.map_annotations = [
            item for item in result.structured_annotations.map_annotations if item.id != annotation_id
        ]
        result.images[index].annotation_refs = [
            item for item in result.images[index].annotation_refs if item.id != annotation_id
        ]
        retained = replace(
            c.resolve_unmixing_input_metadata(ome, index), spectral_state=spectral_state,
            state_source="reviewed_acquisition_completion", correction_mode=correction_mode,
        )
        annotation = c._ensure_annotation(
            result, annotation_id=annotation_id, namespace=c.UNMIXING_INPUT_NAMESPACE,
            value=c._encode_unmixing_input_metadata(retained, channel_ids=tuple(data["channel_ids"])),
        )
        c._link_once(result.images[index].annotation_refs, annotation.id)
    else:
        _install(result, index, data, completed, spectral_state, proof, correction_mode=correction_mode)
    _put(result, index, REVIEW_NS, "CalibrationReview", proof)
    return result


def calibration_review(ome, index=0):
    """

    Reads the saved values and explanation from an image's calibration review.

    Args:
        ome: Metadata containing any calibration review.
        index: Image position, defaulting to zero.

    Returns:
        The review record, or None when no review is linked.
    """

    return _find(ome, index, REVIEW_NS)


def record_acquisition_confirmation(ome, *, source_note, reviewer, index=0):
    """Record matching imaging settings without changing complete numeric metadata.

    Keep any earlier calibration values and reviewer; the appended note identifies
    the new conditions review. Sources without a prior review use the existing
    calibration-review format with no entered numeric values.
    """
    acquisition = c.resolve_unmixing_input_metadata(ome, index)
    if acquisition is None or acquisition.spectral_state != "raw":
        raise ValueError("Imaging-settings confirmation requires complete raw metadata")
    if not isinstance(source_note, str) or not source_note.strip() or not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("Record the imaging settings and their reviewer")
    proof = calibration_review(ome, index)
    if proof is None:
        data = _find(ome, index, PENDING_NS) or _extract(ome, index)
        proof = {"schema_version": "calibration-review/v1", "input_sha256": _digest(data),
                 "values": {}, "reviewer": reviewer.strip(), "spectral_state": "raw",
                 "correction_mode": acquisition.correction_mode}
    else:
        proof = dict(proof)
        if not source_note.startswith(proof["source_note"]):
            raise ValueError("Retain the original calibration source note")
    proof["source_note"] = source_note.strip()
    result = ome.model_copy(deep=True)
    annotation_id = f"Annotation:CalibrationReview:{index}"
    if result.structured_annotations is not None:
        result.structured_annotations.map_annotations = [
            item for item in result.structured_annotations.map_annotations if item.id != annotation_id
        ]
        result.images[index].annotation_refs = [
            item for item in result.images[index].annotation_refs if item.id != annotation_id
        ]
    _put(result, index, REVIEW_NS, "CalibrationReview", proof)
    return result


def replay_calibration(source, reviewed, index=0):
    """

    Reapplies a saved calibration completion when the source imaging settings still match.

    Compare the freshly read acquisition checksum with the checksum retained during
    review. A match allows the saved values to pass through complete_calibration()
    again, keeping the original source note and reviewer with the reconstructed settings.

    Args:
        source: Fresh source metadata obtained when the image was reopened.
        reviewed: Metadata containing the saved calibration review.
        index: Image position in both metadata trees.

    Returns:
        Completed metadata, or source when there is no saved review.

    Raises:
        MetadataConflictError: The original imaging settings or review format differ.
        ValueError: The saved completion fails the current field checks.
    """

    proof = calibration_review(reviewed, index)
    if proof is None or calibration_review(source, index) == proof:
        # An imported source can already carry this exact completed review.
        # Its unchanged acquisition record is compared with reviewed metadata below.
        return source
    data = _find(source, index, PENDING_NS) or _extract(source, index)
    existing = calibration_review(source, index)
    acquisition = c.resolve_unmixing_input_metadata(source, index)
    if acquisition is not None and acquisition.spectral_state == "raw" and (
        (existing is not None and {k: v for k, v in existing.items() if k != "source_note"}
         == {k: v for k, v in proof.items() if k != "source_note"})
        or (existing is None and proof.get("values") == {} and proof.get("input_sha256") == _digest(data)
            and proof.get("spectral_state") == "raw" and proof.get("correction_mode") == acquisition.correction_mode
            and proof.get("schema_version") == "calibration-review/v1")
    ):
        return record_acquisition_confirmation(source, source_note=proof["source_note"],
                                               reviewer=proof["reviewer"], index=index)
    if proof.get("schema_version") != "calibration-review/v1" or proof.get(
        "input_sha256"
    ) != _digest(data):
        raise c.MetadataConflictError(
            "Calibration review does not match the detected image metadata"
        )
    return complete_calibration(
        source,
        proof["values"],
        source_note=proof["source_note"],
        reviewer=proof["reviewer"],
        spectral_state=proof["spectral_state"],
        correction_mode=proof["correction_mode"],
        index=index,
    )




def acquisition_for_correction_mode(ome, mode, index=0):
    """Derive a reference's imaging settings under the target's chosen correction.

    Complete reference metadata can follow an exposure-only target. The source
    values remain unchanged, including absent unapplied factors. Full and exposure-only
    correction require actual exposure per channel. As-stored references must independently
    record the normalized convention; a target cannot establish their correction history.
    """
    current = c.resolve_unmixing_input_metadata(ome, index)
    data = _find(ome, index, PENDING_NS)
    representation = current.intensity_representation if current is not None else (data or {}).get("intensity_representation", "unknown")
    message = raw_intensity_error(representation)
    if message is not None:
        raise c.MetadataConflictError(message)
    if mode == "as_stored":
        if current is None or current.correction_mode != "as_stored" or representation != "normalized_counts":
            raise c.MetadataConflictError("The reference must independently record the same normalized intensity convention as the target")
        return current
    if representation == "normalized_counts":
        raise c.MetadataConflictError("Already-normalized reference values require an as-stored target convention")
    if mode == "full":
        if current is None or current.correction_mode != "full":
            raise c.MetadataConflictError("Full correction requires complete reviewed reference image metadata")
        return current
    if mode != "exposure_only":
        raise ValueError("Unsupported reference correction mode")
    if current is not None:
        return replace(current, correction_mode=mode)
    data = _find(ome, index, PENDING_NS) or _extract(ome, index)
    if any(value is None for value in data["values"]["exposure_seconds"]):
        raise c.MetadataConflictError("Exposure-only correction requires the actual exposure of every reference channel")
    result = ome.model_copy(deep=True)
    _install(result, index, data, data["values"], data["spectral_state"], None, correction_mode=mode)
    return c.resolve_unmixing_input_metadata(result, index)
