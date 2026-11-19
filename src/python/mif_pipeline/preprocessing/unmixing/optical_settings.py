"""Resolve recorded display groups from already-opened metadata, never from markers.

OME IDs identify objects within a source document; these groups do not establish
cross-image optical compatibility. Filter passbands never supply sampled values.
"""
import hashlib
import json
import math

from .reference_models import SpectralOpticalSetting


def _setting(identity, label, provenance):
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return SpectralOpticalSetting(
        "source-optics:" + hashlib.sha256(encoded.encode()).hexdigest(), label.strip(), provenance)


def _im3_settings(ome, channels, scope):
    """Use the existing reader's versioned, image-linked acquisition proof."""
    if ome is None or len(ome.images) != 1 or ome.structured_annotations is None:
        return None
    from mif_pipeline.io.metadata.calibration import read_original_metadata
    from mif_pipeline.io.backends.im3_metadata import _KEY, _NAMESPACE
    image = ome.images[0]
    linked = {ref.id for ref in image.annotation_refs}
    if not any(a.id == "Annotation:IM3BinaryAcquisition:0" and a.id in linked
               and a.namespace == _NAMESPACE for a in ome.structured_annotations.xml_annotations):
        return None
    try:
        raw = read_original_metadata(ome, {_KEY}).get(_KEY)
        if raw is None:
            return None
        proof = json.loads(raw)
        if (proof.get("schema") != "mif-im3-binary-acquisition/v1"
                or proof.get("reader") != "loci.formats.in.IM3Reader"):
            return None
        bands = proof["bands"]
        if len(bands) != len(channels):
            return None
        result = []
        for channel, band in zip(channels, bands, strict=True):
            excitation, emission = band["excitation_filter"], band["emission_filter"]
            if not all(type(v) is str and v.strip() for v in (excitation, emission)):
                return None
            wavelength = band["wavelength_nm"]
            if (type(wavelength) not in (int, float) or not math.isfinite(wavelength)
                    or wavelength <= 0 or channel.emission_wavelength is None
                    or not math.isclose(float(channel.emission_wavelength_quantity.to("nm").magnitude),
                                        wavelength, rel_tol=1e-12, abs_tol=0)):
                return None
            result.append(_setting((scope, "im3", excitation, emission),
                                   excitation.strip(), "im3_acquisition"))
        return tuple(result)
    except (ValueError, TypeError, KeyError, AttributeError):
        # Display-only optional metadata must not prevent source review.
        return None


def recorded_optical_settings(channels, *, ome=None):
    """Return one setting or None per channel, using explicit OME links first.

    Ordered LightPath references describe used optics. FilterSetRef describes
    only the declared set and is labelled accordingly. Known source, excitation
    and detector differences split groups, but exposure/gain do not. Neither an
    excitation wavelength nor a detector by itself establishes an optical group.
    """
    channels = tuple(channels)
    image = ome.images[0] if ome is not None and len(ome.images) == 1 else None
    instrument_id = image.instrument_ref.id if image is not None and image.instrument_ref else None
    scope = (image.id if image else None, instrument_id)
    instrument = next((i for i in ome.instruments if i.id == instrument_id), None) if ome else None
    filters = {f.id: f.model or f.id for f in instrument.filters} if instrument else {}
    sets = {s.id: s.model or s.id for s in instrument.filter_sets} if instrument else {}
    native = _im3_settings(ome, channels, scope)
    result = []
    for index, channel in enumerate(channels):
        path = channel.light_path
        excitation = tuple(f.id for f in path.excitation_filters) if path else ()
        emission = tuple(f.id for f in path.emission_filters) if path else ()
        dichroic = path.dichroic_ref.id if path and path.dichroic_ref else None
        filter_set = channel.filter_set_ref.id if channel.filter_set_ref else None
        light_source = channel.light_source_settings
        excitation_nm = (float(channel.excitation_wavelength_quantity.to("nm").magnitude)
                         if channel.excitation_wavelength is not None else None)
        source_wavelength = (float(light_source.wavelength_quantity.to("nm").magnitude)
                             if light_source and light_source.wavelength is not None else None)
        context = (scope, light_source.id if light_source else None, excitation_nm, source_wavelength,
                   channel.detector_settings.id if channel.detector_settings else None)
        if excitation or emission or dichroic:
            parts = []
            if excitation:
                parts.append("Ex: " + " / ".join(filters.get(ref, ref) for ref in excitation))
            if emission:
                parts.append("Em: " + " / ".join(filters.get(ref, ref) for ref in emission))
            if not parts:
                parts.append("Dichroic: " + dichroic)
            result.append(_setting((context, excitation, dichroic, emission, filter_set),
                                   "; ".join(parts), "ome_light_path"))
        elif filter_set:
            result.append(_setting((context, filter_set), "Filter set: " + sets.get(filter_set, filter_set),
                                   "ome_filter_set"))
        else:
            result.append(native[index] if native else None)
    if image is not None:
        from mif_pipeline.io.metadata.cycles import resolve_channel_cycles
        cycles = resolve_channel_cycles(ome)
        if cycles is not None:
            by_channel = dict(zip((c.id for c in image.pixels.channels), cycles, strict=True))
            show_cycle = len({cycle for cycle in cycles if cycle is not None}) > 1
            for index, channel in enumerate(channels):
                setting, cycle = result[index], by_channel.get(channel.id)
                if setting is not None and cycle is not None:
                    label = setting.label + (f" [cycle {cycle + 1}]" if show_cycle else "")
                    result[index] = _setting((setting.setting_id, "imaging_cycle", cycle),
                                             label, setting.provenance)
    return tuple(result)
