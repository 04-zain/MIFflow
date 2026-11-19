"""Reuse a supplied spectral basis on explicitly recorded cycle bands.

Cycle identity belongs to the reviewed channels and output component inventory.
The supplied spectra retain their own identity; only their run basis is expanded.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from ome_types import from_xml

from mif_pipeline.io.metadata.canonical import is_autofluorescence_component
from mif_pipeline.io.metadata.cycles import (
    cycle_component_name, resolve_channel_cycles, split_cycle_component,
)
from .compatibility import compare_quantitative_transformability
from .reference_models import SpectralMeasurementAxis


@dataclass(frozen=True, slots=True)
class SharedCycleReferenceLayout:
    band_names: tuple[str, ...]
    component_names: tuple[str, ...]
    output_component_names: tuple[str, ...]
    cycles: tuple[int, ...]
    band_indices: tuple[tuple[int, ...], ...]


def shared_cycle_reference_layout(target, af_behavior="retain"):
    """Match exact band identities and known wavelengths across reviewed cycles.

    Remove only the canonical cycle decoration when it agrees with the actual
    channel-bound cycle. Other text remains literal. Band identities must be
    unique within each cycle and each cycle must have the same set.
    """
    xml = target.locator_binding.reviewed_ome_xml
    if xml is None:
        return None
    channel_cycles = resolve_channel_cycles(from_xml(xml), require_complete=True)
    if channel_cycles is None or len(set(channel_cycles)) < 2:
        return None
    if af_behavior not in {"retain", "already_removed"}:
        raise ValueError("The target AF choice is unsupported")
    cycles = tuple(dict.fromkeys(channel_cycles))
    axis = target.evidence.axis
    if len(axis.measurements) != len(channel_cycles):
        raise ValueError("The cycle mapping does not cover the acquired bands")
    names = []
    for measurement, cycle in zip(axis.measurements, channel_cycles, strict=True):
        name, declared = split_cycle_component(measurement.parent_band_id)
        if declared is not None and declared != cycle:
            raise ValueError("An acquired-band label conflicts with its recorded imaging cycle")
        names.append(name)
    indices = []
    bands = None
    for cycle in cycles:
        local = tuple(i for i, value in enumerate(channel_cycles) if value == cycle)
        local_names = tuple(names[i] for i in local)
        if len(set(local_names)) != len(local_names):
            raise ValueError(f"Cycle {cycle + 1} needs unique acquired-band identities")
        if bands is None:
            bands = local_names
        elif set(local_names) != set(bands):
            raise ValueError(f"The acquired bands in cycle {cycle + 1} differ from the shared reference bands")
        indices.append(tuple(local[local_names.index(name)] for name in bands))

    quantitative = target.evidence.quantitative_signature
    reference_indices = indices[0]
    reference_axis = SpectralMeasurementAxis(tuple(
        replace(axis.measurements[i], parent_band_id=names[i]) for i in reference_indices))
    reference_signature = replace(quantitative,
        measurements=tuple(quantitative.measurements[i] for i in reference_indices))
    for cycle, ordered in zip(cycles[1:], indices[1:], strict=True):
        for left, right in zip(reference_indices, ordered, strict=True):
            first, current = axis.measurements[left], axis.measurements[right]
            # The containing axis already requires a common wavelength unit.
            if (first.wavelength is not None and current.wavelength is not None
                    and not math.isclose(first.wavelength, current.wavelength, rel_tol=1e-12)):
                raise ValueError(f"The recorded wavelength for {names[right]} differs in cycle {cycle + 1}")
        current_axis = SpectralMeasurementAxis(tuple(
            replace(axis.measurements[i], parent_band_id=names[i]) for i in ordered))
        current_signature = replace(quantitative,
            measurements=tuple(quantitative.measurements[i] for i in ordered))
        # Normalized stored counts already include acquisition correction; their
        # original exposure/gain do not cause a second correction or a mismatch.
        if reference_signature.measurements[0].correction_mode != "as_stored":
            report = compare_quantitative_transformability(
                reference_signature=reference_signature, candidate_signature=current_signature,
                reference_axis=reference_axis, candidate_axis=current_axis)
            if not report.transformable:
                conflicts = ", ".join(item.field_name for item in report.fields if not item.accepted)
                raise ValueError(f"Cycle {cycle + 1} cannot use the shared reference correction: {conflicts}")

    outputs = tuple(name for name in target.component_names
                    if af_behavior == "retain" or not is_autofluorescence_component(name))
    for name in outputs:
        if split_cycle_component(name)[1] not in cycles:
            raise ValueError("Assign every output component to its imaging cycle in Channels and markers")
    dyes = tuple(dict.fromkeys(split_cycle_component(name)[0] for name in outputs
                              if not is_autofluorescence_component(name)))
    af = tuple(name for name in outputs if is_autofluorescence_component(name))
    if af_behavior == "retain":
        if not af:
            af = tuple(cycle_component_name("Sample AF", cycle) for cycle in cycles)
            outputs += af
        if (len(af) != len(cycles)
                or {split_cycle_component(name)[1] for name in af} != set(cycles)):
            raise ValueError("Retained autofluorescence needs one output component for each imaging cycle")
    shared_af = tuple(dict.fromkeys(split_cycle_component(name)[0] for name in af))
    return SharedCycleReferenceLayout(bands, dyes + shared_af, outputs, cycles, tuple(indices))


def expand_shared_curated_library(library, target, af_behavior="retain"):
    """Expand a retained shared table into the existing whole-target basis contract."""
    if library.band_names == target.evidence.axis.parent_band_ids:
        return library
    layout = shared_cycle_reference_layout(target, af_behavior)
    if layout is None or library.band_names != layout.band_names:
        raise ValueError("The curated library measurement bands do not match this image")
    if library.component_names != layout.component_names:
        raise ValueError("The shared library components differ from the reviewed fluorophore panel")
    if library.measurement_convention == "target_raw_acquisition":
        measurements = target.evidence.quantitative_signature.measurements
        def factor(index):
            item = measurements[index]
            if item.correction_mode == "exposure_only":
                return 1.0 / item.exposure_seconds
            return item.response_correction_multiplier / (
                item.effective_digital_maximum * item.gain * item.binning_area * item.exposure_seconds)
        baseline = tuple(factor(index) for index in layout.band_indices[0])
        if any(tuple(factor(index) for index in ordered) != baseline
               for ordered in layout.band_indices[1:]):
            raise ValueError("Shared raw-intensity spectra require matching per-band intensity-correction factors across cycles. "
                             "Supply fully corrected spectra when those factors differ.")
    import numpy as np
    from .curated_library import CuratedSpectralLibrary
    matrix = np.zeros((len(target.evidence.axis.measurements), len(layout.output_component_names)))
    source = np.asarray(library.values)
    for column, name in enumerate(layout.output_component_names):
        base, cycle = split_cycle_component(name)
        source_column = library.component_names.index(base)
        rows = layout.band_indices[layout.cycles.index(cycle)]
        matrix[list(rows), column] = source[:, source_column]
    return CuratedSpectralLibrary(target.evidence.axis.parent_band_ids, layout.output_component_names,
        tuple(tuple(float(value) for value in row) for row in matrix),
        library.normalization, library.measurement_convention)
