"""Reconstruct a bounded field from retained fitted components, without refitting."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
import json
import math
from pathlib import Path
import re

import numpy as np

MAX_SIDE = 512
FIGURE_ID = "reconstruction-diagnostic"
SPECTRA_FIGURE_ID = "reconstruction-spectra"
MATCHED_FIGURE_ID = "acquired-components"
FIELD_FIGURE_IDS = frozenset((FIGURE_ID, SPECTRA_FIGURE_ID, MATCHED_FIGURE_ID))
MAX_MATCHED_PANELS = 3


@dataclass
class ReconstructionRecord:
    source_authority: object
    ome: object
    basis: object
    provenance: dict
    image_path: Path
    size_x: int
    size_y: int
    scale: tuple[float, float]
    bands: tuple[str, ...]
    metadata: dict


def reconstruction_record(authority, snapshot, basis=None):
    """Resolve this accepted generation, not a later publication or pending review."""
    from .saved_reference_figures import reference_operation_root, reference_basis
    from .reference_unmixing import ReferenceUnmixingRequest
    from mif_pipeline.io.processed_image import ProcessedImage
    from mif_pipeline.io.canonical_ome_tiff import _physical_calibration
    from mif_pipeline.preprocessing.unmixing.output_publication_records import manifest_from_bytes

    ref = authority.artifact_ref
    if ref.artifact_kind != "processed_image":
        raise ValueError("Select a prepared-image result for reconstruction.")
    basis = basis if basis is not None else reference_basis(authority, snapshot)
    if basis is None:
        raise ValueError("Reconstruction requires a saved unmixing reference library.")
    root = reference_operation_root(authority, snapshot)
    request = ReferenceUnmixingRequest.from_mapping(json.loads((root / "reference-unmixing-request.json").read_bytes()))
    source, ome = request.reviewed_source_binding()
    image_path = Path(ref.locator)
    manifest = manifest_from_bytes((image_path.parent / "manifest.json").read_bytes())
    records = {item.role: item for item in manifest.artifacts}
    image_record, provenance_record = records["image"], records["provenance"]
    provenance_raw = (image_path.parent / provenance_record.relative_name).read_bytes()
    if image_path.name != image_record.relative_name or image_record.content_sha256 != ref.sha256:
        raise ValueError("The saved image entry does not identify the selected result.")
    provenance = json.loads(provenance_raw)
    components = tuple(c.component_identity for c in basis.proposal.columns)
    if (provenance["input_sha256"] != source.source_identity.file_sha256
            or provenance["input_sha256"] != ref.owning_identity["target_image_sha256"]
            or provenance["resolved_basis_authority"]["authority_sha256"] != basis.content_sha256):
        raise ValueError("The source and reference library do not match this prepared image.")
    if tuple(provenance["output_component_order"]) != components:
        raise ValueError("Reconstruction is unavailable because the saved image does not retain every fitted component, including fitted autofluorescence.")
    bands = tuple(provenance["input_band_order"])
    if bands != tuple(basis.proposal.axis.parent_band_ids):
        raise ValueError("The acquired bands differ from the saved reference library.")
    pixels = ome.images[0].pixels
    if pixels.size_z != 1 or pixels.size_t != 1 or pixels.size_c != len(bands):
        raise ValueError("Field figures require a saved single-plane source with an explicit acquired-band mapping.")
    from mif_pipeline.io.metadata.cycles import resolve_channel_cycles
    input_cycles = tuple(provenance.get("input_imaging_cycles", ()))
    output_cycles = tuple(provenance.get("output_imaging_cycles", ()))
    if (resolve_channel_cycles(ome, require_complete=True) or ()) != input_cycles:
        raise ValueError("The source cycle mapping differs from the saved unmixing result.")
    markers = basis.biomarkers
    if tuple(provenance.get("output_biomarkers") or markers) != markers:
        raise ValueError("The saved component markers differ from the accepted reference library.")
    with ProcessedImage(image_path, cache_bytes=0) as image:
        level = image.levels[0]
        size_c, size_y, size_x = level.shape
        scale = (level.x_um_per_px, level.y_um_per_px)
        if input_cycles or output_cycles:
            from ome_types import from_xml
            output_ome = from_xml(image.attributes["mif_pipeline"]["ome_xml"])
            if (len(output_cycles) != len(components)
                    or resolve_channel_cycles(output_ome, require_complete=True) != output_cycles):
                raise ValueError("The saved component cycle mapping is unavailable or inconsistent.")
    if size_c != len(components):
        raise ValueError("Reconstruction is unavailable because a fitted component is missing from the saved image.")
    source_scale = (
        _physical_calibration(pixels.physical_size_x, pixels.physical_size_x_unit, "X")[2],
        _physical_calibration(pixels.physical_size_y, pixels.physical_size_y_unit, "Y")[2],
    )
    if ((size_x, size_y) != (pixels.size_x, pixels.size_y)
            or None in source_scale or not np.allclose(scale, source_scale, rtol=1e-12, atol=0)):
        raise ValueError("The source and prepared image have different dimensions or pixel sizes.")
    component_details = [dict(identity=column.component_identity, biomarker=marker,
                        fluorophore=column.fluorophore_identity, kind=column.component_kind)
                         for column, marker in zip(basis.proposal.columns, markers, strict=True)]
    metadata = dict(source_sha256=source.source_identity.file_sha256, output_sha256=ref.sha256,
                    basis_sha256=basis.content_sha256, components=components, bands=bands,
                    component_details=component_details,
                    input_imaging_cycles=input_cycles, output_imaging_cycles=output_cycles,
                    canonical_source_plane=dict(series=0, z=0, t=0),
                    source_series_index=source.selection.source_series_index,
                    normalization_formula=provenance["normalization_formula"],
                    normalization_policy=provenance["normalization_policy"],
                    normalization_divisors=provenance["normalization_divisors"],
                    applied_offsets=provenance["applied_offsets"],
                    response_correction_multipliers=provenance["response_correction_multipliers"],
                    reporting_multipliers=provenance["spectral_column_sums"],
                    component_intensity_semantic="Saved reported component intensity; fitted coefficient multiplied by the complete reporting multiplier",
                    pixel_size_xy_um=scale)
    return ReconstructionRecord(source, ome, basis, provenance, image_path, size_x, size_y, scale, bands, metadata)


def reconstruction_choice(authority, snapshot, basis):
    """Offer the field settings without opening vendor pixels."""
    from .batch_service_models import BatchSavedStageFigure
    from mif_pipeline.io.processed_image import ProcessedImageError
    from mif_pipeline.io import MetadataConflictError
    try:
        record = reconstruction_record(authority, snapshot, basis)
    except (OSError, ValueError, KeyError, ProcessedImageError, MetadataConflictError) as exc:
        return (), f"Reconstruction diagnostic unavailable: {exc}"
    defaults = dict(x=0, y=0, width=min(MAX_SIDE, record.size_x), height=min(MAX_SIDE, record.size_y),
                    band_index=0, intensity_min=None, intensity_max=None, residual_max=0.2,
                    band_indices=list(range(min(MAX_MATCHED_PANELS, len(record.bands)))),
                    component_indices=list(range(min(MAX_MATCHED_PANELS, len(record.metadata["components"])))))
    settings = dict(size_x=record.size_x, size_y=record.size_y, max_width=MAX_SIDE, max_height=MAX_SIDE,
                    bands=[dict(index=i, label=name) for i, name in enumerate(record.bands)],
                    components=[dict(index=i, label=_component_label(item))
                                for i, item in enumerate(record.metadata["component_details"])],
                    max_panels=MAX_MATCHED_PANELS, defaults=defaults)
    return (
        BatchSavedStageFigure(MATCHED_FIGURE_ID, "Acquired bands and components",
            "Compare selected acquired bands and saved unmixed components in the same field. "
            "Each panel has independent display limits; the two groups have different intensity scales.",
            settings=settings, data_formats=("csv", "npz")),
        BatchSavedStageFigure(FIGURE_ID, "Reconstruction images",
            "Compare the observed and reconstructed acquired band, alongside the spectral residual across the selected field.",
            settings=settings, data_formats=("csv", "npz")),
        BatchSavedStageFigure(SPECTRA_FIGURE_ID, "Reconstruction mean spectra",
            "Compare mean observed and reconstructed intensity across all acquired bands.",
            settings=settings, data_formats=("csv", "npz")),
    ), ""


def field_selection(record, options, *, figure_id=FIGURE_ID):
    """Return an exact level-zero rectangle from user options and saved dimensions.

    Coordinates must be integer pixels and the whole rectangle must fit the image.
    The current resource bound is 512 pixels per side; invalid or oversized fields
    raise ValueError rather than being cropped. Validate display choices before any
    vendor pixels are opened, so invalid band/component/range settings do not trigger a read.
    """
    defaults = dict(x=0, y=0, width=min(MAX_SIDE, record.size_x), height=min(MAX_SIDE, record.size_y))
    selection = {key: options.get(key, value) for key, value in defaults.items()}
    if any(type(value) is not int for value in selection.values()):
        raise ValueError("Field coordinates and dimensions must be whole numbers.")
    x, y, width, height = (selection[k] for k in ("x", "y", "width", "height"))
    if (x < 0 or y < 0 or not 1 <= width <= MAX_SIDE or not 1 <= height <= MAX_SIDE
            or x + width > record.size_x or y + height > record.size_y):
        raise ValueError("Choose a field inside the image, no larger than 512 × 512 pixels.")
    if figure_id == MATCHED_FIGURE_ID:
        _matched_selection(options, len(record.bands), len(record.metadata["components"]))
    else:
        _display_limits(options, len(record.bands))
    return selection


@dataclass
class ReconstructionField:
    observed: np.ndarray
    reconstructed: np.ndarray
    relative: np.ndarray
    absolute: np.ndarray
    observed_norm: np.ndarray
    metadata: dict
    reported: np.ndarray | None = None


def reconstruction_values(observed, reported, matrix, reporting):
    """Invert the complete reporting multiplier before multiplying by the saved basis."""
    reconstructed = (matrix @ (reported / reporting[:, None, None]).reshape(len(reporting), -1)).reshape(observed.shape)
    norm = np.linalg.norm(observed, axis=0)
    absolute = np.linalg.norm(observed - reconstructed, axis=0)
    relative = np.full(norm.shape, np.nan)
    np.divide(absolute, norm, out=relative, where=norm > 0)
    return reconstructed, relative, absolute, norm


def load_reconstruction(record, selection):
    """Read one admitted source/output field and reconstruct all acquired bands.

    record binds the accepted source, normalization provenance, image and basis;
    selection is the validated x/y/width/height mapping from field_selection.
    The reviewed source exposes its selected native series as canonical series0.
    Production normalization retains its float32 arithmetic, then measurements
    and stored component pixels are promoted to float64 for scale inversion and
    matrix multiplication. This preserves stored values; it cannot recover precision
    lost when fitted components were written to their chosen storage dtype.

    Return a ReconstructionField containing observed/reconstructed band cubes,
    the exact saved reported component cube,
    relative and absolute spectral-L2 residuals, observed norm and export metadata.
    Both readers close on failure. Missing components, changed source identity or
    inconsistent applied normalization are errors; no solver is called.
    """
    from mif_pipeline.io import (open_reviewed_source, resolve_source_image_facts,
        resolve_unmixing_input_metadata, resolve_acquisition_digital_range, PlaneIndex, Region)
    from mif_pipeline.io.processed_image import ProcessedImage
    from mif_pipeline.preprocessing.unmixing.normalization import (
        CanonicalOffsetExposureNormalization, ResponseCorrectionTransform, normalize_region)

    provenance = record.provenance
    matrix = np.asarray([c.values for c in record.basis.proposal.columns], dtype=np.float64).T
    reporting = np.asarray(provenance["spectral_column_sums"], dtype=np.float64)
    expected = np.asarray([math.fsum(c.values) * float(c.abundance_rescaling_factor)
                           for c in record.basis.proposal.columns])
    if not np.array_equal(reporting, expected) or not np.all(np.isfinite(reporting) & (reporting > 0)):
        raise ValueError("The saved component reporting scale differs from the accepted basis.")
    response = ResponseCorrectionTransform(provenance["response_transform_id"],
        provenance["response_transform_semantic"], provenance["response_transform_authority"],
        tuple(provenance["response_measurement_ids"]), tuple(provenance["response_correction_multipliers"]))
    if response.measurement_ids != record.basis.proposal.axis.measurement_ids:
        raise ValueError("The applied response correction has a different acquired-band order.")
    with open_reviewed_source(record.source_authority, record.ome) as source:
        facts = resolve_source_image_facts(source)
        acquisition = resolve_unmixing_input_metadata(record.ome, 0)
        if acquisition is None:
            raise ValueError("The intensity correction used for this saved result is unavailable.")
        factors = CanonicalOffsetExposureNormalization().build_factors(acquisition,
            resolve_acquisition_digital_range(facts, acquisition), response_transform=response)
        if (tuple(acquisition.band_order) != record.bands or factors.formula != provenance["normalization_formula"]
                or factors.policy != provenance["normalization_policy"]
                or not np.array_equal(factors.per_band_divisor, provenance["normalization_divisors"])
                or (None if factors.offsets is None else factors.offsets.tolist()) != provenance["applied_offsets"]):
            raise ValueError("The source correction differs from the correction used for the saved result.")
        region = Region(**selection)
        raw = np.stack([source.read_region(series=0, resolution=0, plane=PlaneIndex(c=c), region=region)
                        for c in range(len(record.bands))])
        observed = normalize_region(raw, factors).astype(np.float64)
        del raw
    with ProcessedImage(record.image_path, cache_bytes=32 * 1024**2) as image:
        reported = image.read_window(0, **selection, max_output_bytes=len(reporting) * selection["width"] * selection["height"] * 8).astype(np.float64)
    reconstructed, relative, absolute, norm = reconstruction_values(observed, reported, matrix, reporting)
    if (not np.all(np.isfinite(observed)) or not np.all(np.isfinite(reported))
            or not np.all(np.isfinite(reconstructed))):
        raise ValueError("The selected field contains non-finite source or component intensities.")
    metadata = dict(record.metadata, **selection, physical_bounds_um=[selection["x"] * record.scale[0], selection["y"] * record.scale[1],
                    (selection["x"] + selection["width"]) * record.scale[0], (selection["y"] + selection["height"]) * record.scale[1]], undefined_pixels=int(np.count_nonzero(norm == 0)),
                    relative_residual="spectral L2 norm(observed - reconstructed) / spectral L2 norm(observed); zero observed norm is undefined",
                    reconstruction="accepted basis @ (saved reported component intensities / complete reporting multipliers)")
    return ReconstructionField(observed, reconstructed, relative, absolute, norm, metadata, reported)


def _component_label(component):
    marker, identity = component["biomarker"], component["identity"]
    return f"{marker} · {identity}" if marker and marker != identity else identity


def _matched_selection(options, band_count, component_count):
    """Validate both independent panel groups before any image pixels are read."""
    result = []
    for key, count, label in (("band_indices", band_count, "acquired bands"),
                              ("component_indices", component_count, "saved components")):
        indices = options.get(key, list(range(min(MAX_MATCHED_PANELS, count))))
        if (not isinstance(indices, (list, tuple)) or not 1 <= len(indices) <= MAX_MATCHED_PANELS
                or any(type(i) is not int or not 0 <= i < count for i in indices)
                or len(set(indices)) != len(indices)):
            raise ValueError(f"Choose one to {MAX_MATCHED_PANELS} distinct {label}.")
        result.append(list(indices))
    return result


def _independent_limits(values):
    low, high = float(min(0, values.min())), float(np.percentile(values, 99.5))
    return [low, high if high > low else low + 1]


def _display_limits(options, band_count):
    band = options.get("band_index", 0)
    if type(band) is not int or not 0 <= band < band_count:
        raise ValueError("Choose an acquired band from the image.")
    low, high = options.get("intensity_min"), options.get("intensity_max")
    residual = options.get("residual_max", 0.2)
    if (type(residual) not in (int, float) or not math.isfinite(residual) or residual <= 0
            or ((low is not None or high is not None) and
                (any(type(v) not in (int, float) or not math.isfinite(v) for v in (low, high)) or high <= low))):
        raise ValueError("Choose finite display limits with maximum above minimum and a positive residual maximum.")
    return band, low, high, residual


def display_options(field, options, *, figure_id=FIGURE_ID):
    """Resolve the recorded display mapping for a retained field.

    Matched panels each use min(0, minimum) and their own 99.5th percentile;
    saved reported component values keep their complete reporting multiplier.
    Reconstruction options select an acquired band and optional finite image limits. With both
    image limits absent, use a shared lower bound of min(0, observed/reconstructed
    minimum) and the joint99.5th percentile as the upper bound. Flat fields receive
    a unit display span. Return the rectangle, band and numerical limits for both
    the renderer and Results controls; scientific arrays remain unchanged.
    """
    if figure_id == MATCHED_FIGURE_ID:
        if (field.reported is None
                or field.reported.shape != (len(field.metadata["components"]), *field.observed.shape[1:])
                or not np.all(np.isfinite(field.reported)) or not np.all(np.isfinite(field.observed))):
            raise ValueError("The selected field does not retain finite pixels for every saved component.")
        bands, components = _matched_selection(options, len(field.observed), len(field.reported))
        return {**{key: field.metadata[key] for key in ("x", "y", "width", "height")},
                "band_indices": bands, "component_indices": components,
                "band_display_limits": [_independent_limits(field.observed[i]) for i in bands],
                "component_display_limits": [_independent_limits(field.reported[i]) for i in components],
                "display_policy": "Independent panels: min(0, minimum) to 99.5th percentile; flat fields use a unit span",
                "colormap": "magma"}
    band, low, high, residual = _display_limits(options, len(field.observed))
    if low is None:
        values = np.concatenate((field.observed[band].ravel(), field.reconstructed[band].ravel()))
        low, high = float(min(0, values.min())), float(np.percentile(values, 99.5))
        if high <= low:
            high = low + 1
    return dict((key, field.metadata[key]) for key in ("x", "y", "width", "height")) | dict(
        band_index=band, intensity_min=float(low), intensity_max=float(high), residual_max=float(residual))


@contextmanager
def reconstruction_resources(snapshot, authority, record, selection):
    """Admit the bounded all-band calculation and figure buffers before pixel reads."""
    from .batch_resources import BatchResourceDemand, ResourceLeasePool
    from .batch_dag import ResourceKind
    from .batch_phenotyping._common import digest
    from mif_pipeline.pipeline_core.execution_resources import operation_budget
    # Include the retained float64 reported cube and selected export-array copies.
    memory_bytes = 192 * 1024**2 + selection["width"] * selection["height"] * (40 * len(record.bands) + 32 * len(record.basis.proposal.columns) + 80)
    demand = BatchResourceDemand(node_id=authority.artifact_ref.producer_node_id,
        adapter_spec_sha256=digest({"operation": "saved-reconstruction-diagnostic"}),
        input_artifact_set_sha256=authority.artifact_ref.artifact_ref_sha256,
        resource_kind=ResourceKind.SCIENTIFIC_COMPUTE, worker_slots=1, future_slots=1,
        working_set_bytes=memory_bytes, cpu_threads=1)
    reservation = ResourceLeasePool(snapshot.manifest.manifest.execution_config).try_acquire(demand)
    if reservation is None:
        raise ValueError("Memory is in use. Wait for processing to finish and open the diagnostic again.")
    try:
        with operation_budget(memory_bytes, 1):
            yield
    finally:
        reservation.release()


def _filter_groups(bands):
    """Group only explicitly labelled instrument filter sets, never component names."""
    groups = []
    for index, label in enumerate(bands):
        match = re.fullmatch(r"(.+?):\s*([0-9.]+)\s*nm", label)
        if match is None:
            return None
        name, wavelength = match.group(1), float(match.group(2))
        if not groups or groups[-1][0] != name:
            groups.append((name, [], []))
        groups[-1][1].append(index)
        groups[-1][2].append(wavelength)
    return groups if len(groups) <= 6 else None


def _signal_label(metadata):
    return ("Exposure-normalized signal" if metadata["normalization_policy"] == "mif_pipeline_exposure_only_v1"
            else "Stored acquired signal" if metadata["normalization_policy"] == "mif_pipeline_as_stored_v1"
            else "Corrected signal")


def _matched_figure(field, options):
    """Two independently labelled panel groups in the same physical coordinate frame."""
    from textwrap import fill
    from matplotlib.figure import Figure
    from matplotlib import patheffects
    metadata = field.metadata
    width_um = metadata["width"] * metadata["pixel_size_xy_um"][0]
    height_um = metadata["height"] * metadata["pixel_size_xy_um"][1]
    columns = max(len(options["band_indices"]), len(options["component_indices"]))
    panel_height_mm = min(80, max(24, 166 / columns * height_um / width_um))
    figure = Figure(figsize=(180 / 25.4, (2 * panel_height_mm + 48) / 25.4),
                    layout="constrained", facecolor="white")
    figure.get_layout_engine().set(w_pad=4 / 72, h_pad=4 / 72, hspace=.04, wspace=.04)
    grid = figure.add_gridspec(4, columns, height_ratios=(.13, 1, .13, 1))
    span = min(width_um, height_um) / 4
    decade = 10 ** math.floor(math.log10(span))
    length = max(value * decade for value in (1, 2, 5) if value * decade <= span)
    groups = (("Acquired bands · " + _signal_label(metadata).lower(), field.observed,
               options["band_indices"], metadata["bands"], options["band_display_limits"]),
              ("Unmixed components · intensity", field.reported,
               options["component_indices"], tuple(_component_label(c) for c in metadata["component_details"]),
               options["component_display_limits"]))
    panel = 0
    for row, (heading, cube, indices, labels, limits) in enumerate(groups):
        heading_ax = figure.add_subplot(grid[row * 2, :])
        heading_ax.set_axis_off()
        heading_ax.text(0, .2, heading, transform=heading_ax.transAxes, fontsize=9, va="bottom", color="#222222")
        for column, (index, (low, high)) in enumerate(zip(indices, limits, strict=True)):
            ax = figure.add_subplot(grid[row * 2 + 1, column])
            values = cube[index]
            image = ax.imshow(values, cmap=options["colormap"], vmin=low, vmax=high,
                              extent=(0, width_um, height_um, 0), interpolation="nearest")
            ax.set_title(fill(f"{chr(65 + panel)}  {labels[index]}", width=32), loc="left", fontsize=8, pad=5)
            ax.set_xticks([]); ax.set_yticks([])
            for spine in ax.spines.values():
                spine.set_visible(False)
            start, baseline = width_um * .06, height_um * .92
            stroke = [patheffects.withStroke(linewidth=2.8, foreground="#222222")]
            ax.plot([start, start + length], [baseline] * 2, color="white", lw=1.8, path_effects=stroke)
            ax.text(start + length / 2, baseline - height_um * .035, f"{length:g} µm",
                    ha="center", va="bottom", color="white", fontsize=7,
                    path_effects=[patheffects.withStroke(linewidth=1.5, foreground="#222222")])
            below, above = np.any(values < low), np.any(values > high)
            extend = "both" if below and above else "min" if below else "max" if above else "neither"
            bar = figure.colorbar(image, ax=ax, orientation="horizontal", fraction=.065, pad=.035,
                                  aspect=30, extend=extend)
            bar.set_ticks([low, high], labels=[f"{low:.3g}", f"{high:.3g}"])
            bar.ax.tick_params(labelsize=7, length=2.5, width=.5, pad=2)
            bar.outline.set_linewidth(.5)
            panel += 1
    figure.supxlabel("Each panel has independent display limits.", fontsize=7, color="#444444")
    return figure


def _matched_data(field, options, metadata):
    """Export the selected, unscaled scientific arrays and their exact display mapping."""
    from mif_pipeline.cell_analysis.result_plots import _csv_output
    rows = []
    groups = (("acquired_band", field.observed, options["band_indices"], metadata["bands"],
               options["band_display_limits"], _signal_label(metadata)),
              ("reported_component", field.reported, options["component_indices"],
               tuple(_component_label(c) for c in metadata["component_details"]),
               options["component_display_limits"], "Saved reported component intensity"))
    for group, cube, indices, labels, limits, semantic in groups:
        for index, (low, high) in zip(indices, limits, strict=True):
            values = cube[index]
            rows.append((group, index, labels[index], semantic, float(values.mean()), float(values.min()),
                         float(values.max()), low, high, metadata["x"], metadata["y"],
                         metadata["width"], metadata["height"], *metadata["pixel_size_xy_um"],
                         metadata["source_sha256"], metadata["output_sha256"], metadata["basis_sha256"]))
    table = _csv_output(MATCHED_FIGURE_ID, "Acquired bands and components · Panel data",
        ("panel_group", "channel_index", "label", "intensity_semantic", "mean", "minimum", "maximum",
         "display_min", "display_max", "x", "y", "width", "height", "pixel_size_x_um", "pixel_size_y_um",
         "source_sha256", "output_sha256", "basis_sha256"), rows)
    buffer = BytesIO()
    np.savez_compressed(buffer, observed_bands=field.observed[options["band_indices"]],
        reported_components=field.reported[options["component_indices"]],
        band_indices=np.asarray(options["band_indices"]), component_indices=np.asarray(options["component_indices"]),
        metadata_json=np.asarray(json.dumps(metadata, sort_keys=True)))
    return table, (MATCHED_FIGURE_ID + "/data", "Acquired bands and components · Field data", "npz", buffer.getvalue())


def _image_figure(field, options):
    """Retain the original three-panel comparison at 180 mm export width."""
    from matplotlib.figure import Figure
    from matplotlib.colors import Normalize
    from matplotlib import colormaps
    band = options["band_index"]
    metadata = field.metadata
    width_um = metadata["width"] * metadata["pixel_size_xy_um"][0]
    height_um = metadata["height"] * metadata["pixel_size_xy_um"][1]
    scale = (180 / 25.4) / 11.6
    figure = Figure(figsize=(180 / 25.4, 4.2 * scale), layout="constrained", facecolor="white")
    figure.get_layout_engine().set(w_pad=3 / 72 * scale, h_pad=3 / 72 * scale)
    axes = figure.subplots(1, 3)
    norm = Normalize(options["intensity_min"], options["intensity_max"])
    label = re.sub(r"(?<=\d)nm$", " nm", metadata["bands"][band])
    span = min(width_um, height_um) / 4
    decade = 10 ** math.floor(math.log10(span))
    length = max(value * decade for value in (1, 2, 5) if value * decade <= span)
    titles = (f"A  Observed · {label}", f"B  Reconstructed · {label}", "C  Relative spectral residual")
    cmap = colormaps["viridis"].copy(); cmap.set_bad("#bcbcbc")
    panels = []
    for ax, values, title, colour, limits in zip(axes,
            (field.observed[band], field.reconstructed[band], field.relative),
            titles, ("magma", "magma", cmap), (norm, norm, Normalize(0, options["residual_max"])), strict=True):
        panels.append(ax.imshow(values, cmap=colour, norm=limits, extent=(0, width_um, height_um, 0), interpolation="nearest"))
        ax.set_title(title, fontsize=8, pad=8 * scale)
        ax.set_xticks([]); ax.set_yticks([])
        ax.plot([length * .3, length * .3 + length],
                [height_um - length * .4] * 2, color="white", lw=3 * scale)
        ax.text(length * .3 + length / 2, height_um - length * .52,
                f"{length:g} µm", ha="center", color="white", fontsize=7)
    if metadata["undefined_pixels"]:
        from matplotlib.patches import Patch
        axes[2].legend(handles=[Patch(facecolor="#bcbcbc", label="Undefined")],
                       loc="upper right", fontsize=7, framealpha=.9, edgecolor="none")
    # Allocate bars as in the original image-only figure, preserving equal panels.
    below = any(np.any(values < options["intensity_min"]) for values in (field.observed[band], field.reconstructed[band]))
    above = any(np.any(values > options["intensity_max"]) for values in (field.observed[band], field.reconstructed[band]))
    extend = "both" if below and above else "min" if below else "max" if above else "neither"
    bar = figure.colorbar(panels[0], ax=list(axes[:2]), orientation="horizontal", fraction=.07, pad=.035, extend=extend)
    bar.set_label(_signal_label(metadata), fontsize=7)
    bar.formatter.set_powerlimits((0, 0)); bar.update_ticks()
    bar.ax.tick_params(labelsize=7)
    bar.ax.xaxis.get_offset_text().set_fontsize(7)
    residual_extend = "max" if np.any(field.relative > options["residual_max"]) else "neither"
    bar = figure.colorbar(panels[2], ax=axes[2], orientation="horizontal", fraction=.07, pad=.035, extend=residual_extend)
    bar.set_label("Spectral L2 residual / observed L2", fontsize=7)
    bar.set_ticks(np.linspace(0, options["residual_max"], 5)); bar.ax.tick_params(labelsize=7)
    return figure


def _spectra_figure(means, metadata):
    """Plot retained field means at 180 mm width with fixed 7–9 pt typography.

    means contains observed/reconstructed arithmetic pixel means for each band;
    metadata supplies the recorded band labels and applied correction policy.
    Both rows show the same values and common intensity scale. This renderer
    changes only their appearance, never the reduction or correction calculation.
    """
    from matplotlib.figure import Figure
    from matplotlib.font_manager import FontProperties
    from matplotlib.text import Text
    from matplotlib.ticker import MaxNLocator
    font_path = Path(__file__).resolve().parents[1] / "cell_analysis/data/fonts/LiberationSans-Regular.ttf"
    bands = metadata["bands"]
    groups = _filter_groups(bands)
    figure = Figure(figsize=(180 / 25.4, (105 if groups else 60) / 25.4), facecolor="white")
    layout = figure.add_gridspec(2 if groups else 1, 1, left=.095, right=.985,
                                bottom=.115 if groups else .23, top=.89 if groups else .80,
                                height_ratios=(1.25, 1) if groups else (1,), hspace=.56)
    overview = figure.add_subplot(layout[0])
    peak = float(means.max())
    minimum = float(means.min())
    magnitude = max(abs(minimum), abs(peak))
    exponent = int(math.floor(math.log10(magnitude))) if magnitude > 0 else 0
    exponent = exponent if abs(exponent) >= 3 else 0
    divisor = 10**exponent
    limit = max(peak / divisor * 1.22, 1e-12)
    lower = min(0.0, minimum / divisor * 1.08)
    policy = metadata["normalization_policy"]
    ylabel = ("Mean pixel intensity / exposure time" if policy == "mif_pipeline_exposure_only_v1"
              else "Mean stored pixel intensity" if policy == "mif_pipeline_as_stored_v1"
              else "Mean corrected pixel intensity")
    if exponent:
        power = str(exponent).translate(str.maketrans("-0123456789", "⁻⁰¹²³⁴⁵⁶⁷⁸⁹"))
        ylabel += f"\n(×10{power})"
    colours = ("#196a9e", "#cc6937")
    def curves(ax, x, positions):
        for values, colour, line, marker, label in zip(means, colours, ("-", "--"), ("o", "s"),
                ("Observed", "Reconstructed"), strict=True):
            ax.plot(x, values[positions] / divisor, color=colour, lw=1.0, ls=line, marker=marker,
                    ms=2.2, markeredgewidth=.5, markerfacecolor="white" if marker == "s" else colour,
                    label=label, zorder=3)
        ax.set_ylim(lower, limit)
        ax.yaxis.set_major_locator(MaxNLocator(4))
        ax.grid(False)
        ax.tick_params(length=2.5, width=.55, pad=3, labelsize=7, colors="#222222")
        ax.spines['left'].set_color('#222222'); ax.spines['bottom'].set_color('#222222')
        ax.spines['left'].set_linewidth(.55); ax.spines['bottom'].set_linewidth(.55)
    positions = np.arange(len(bands))
    curves(overview, positions + 1, positions)
    overview.set_xlim(.5, len(bands) + .5)
    overview.set_xticks(np.unique(np.linspace(1, len(bands), min(8, len(bands)), dtype=int)))
    overview.set_xlabel("Acquired band", labelpad=4, fontsize=8, color="#222222")
    figure.text(.022, .5, ylabel, rotation=90,
                va="center", ha="center", fontsize=8, linespacing=1.25, color="#222222")
    if groups:
        overview.text(0, 1.11, "A", transform=overview.transAxes, fontsize=9, fontweight="normal", color="#222222", va="bottom")
    overview.legend(frameon=False, ncol=2, loc="upper right", bbox_to_anchor=(1, 1.22),
                    handlelength=2.6, handletextpad=.6, columnspacing=1.4, fontsize=7.5, labelcolor="#222222")
    if groups:
        for i, (name, indexes, _) in enumerate(groups):
            left, right = indexes[0] + .5, indexes[-1] + 1.5
            if i:
                overview.axvline(left, color="#d0d0d0", lw=.45, zorder=1)
            overview.text((left + right) / 2, .96, name, transform=overview.get_xaxis_transform(),
                          ha="center", va="top", fontsize=7, color="#333333")
    if groups is None:
        for text in figure.findobj(Text):
            text.set_fontproperties(FontProperties(
                fname=font_path, family=["Liberation Sans", "Arial", "sans-serif"], size=text.get_fontsize()))
        return figure
    separated = layout[1].subgridspec(1, len(groups), wspace=.24)
    for index, (name, indexes, wavelengths) in enumerate(groups):
        ax = figure.add_subplot(separated[0, index])
        curves(ax, wavelengths, indexes)
        ax.set_title(name, fontsize=8, fontweight="normal", color="#222222", loc="left", pad=8)
        ax.set_xticks(sorted(set((wavelengths[0], wavelengths[len(wavelengths) // 2], wavelengths[-1]))))
        ax.tick_params(axis="y", labelleft=index == 0)
        if index == 0:
            ax.text(0, 1.24, "B", transform=ax.transAxes,
                    fontsize=9, fontweight="normal", color="#222222", va="bottom")
    figure.text(.54, .025, "Detection wavelength (nm)", ha="center", fontsize=8, color="#222222")
    for text in figure.findobj(Text):
        text.set_fontproperties(FontProperties(
                fname=font_path, family=["Liberation Sans", "Arial", "sans-serif"], size=text.get_fontsize()))
    return figure


def render_reconstruction(field, options, *, figure_id=FIGURE_ID):
    """Render a saved field view without changing its measurement or reporting scale."""
    from matplotlib import rc_context
    from mif_pipeline.cell_analysis.result_plots import _csv_output
    if figure_id not in FIELD_FIGURE_IDS:
        raise ValueError("Choose an acquired-band/component, reconstruction image or mean-spectrum figure.")
    matched = figure_id == MATCHED_FIGURE_ID
    metadata = dict(field.metadata, display_options=options)
    bands = metadata["bands"]
    means = np.stack((field.observed.mean(axis=(1, 2)), field.reconstructed.mean(axis=(1, 2))))
    scale_x, scale_y = metadata["pixel_size_xy_um"]
    title = ("Acquired bands and components" if matched else
             "Reconstruction images" if figure_id == FIGURE_ID else "Reconstruction mean spectra")
    with rc_context({"font.family": "DejaVu Sans", "font.size": 8, "axes.titlesize": 8,
                     "axes.labelsize": 8, "xtick.labelsize": 7, "ytick.labelsize": 7,
                     "axes.spines.top": False, "axes.spines.right": False,
                     "svg.fonttype": "none", "pdf.fonttype": 42}):
        figure = (_matched_figure(field, options) if matched else
                  _image_figure(field, options) if figure_id == FIGURE_ID else _spectra_figure(means, metadata))
        from mif_pipeline.cell_analysis.figure_style import apply_figure_font
        apply_figure_font(figure)
        outputs = []
        try:
            for fmt in ("png", "pdf", "svg"):
                buffer = BytesIO(); figure.savefig(buffer, format=fmt, dpi=300, facecolor="white")
                outputs.append((figure_id + "/" + fmt, title, fmt, buffer.getvalue()))
        finally:
            figure.clear()
    if matched:
        return tuple(outputs) + _matched_data(field, options, metadata)
    band = options["band_index"]
    outputs.append(_csv_output(figure_id, "Field mean spectra", (
        "band_index", "acquired_band", "observed_mean", "reconstructed_mean", "x", "y", "width", "height", "pixel_size_x_um", "pixel_size_y_um", "source_sha256", "output_sha256", "basis_sha256"),
        ((i, label, means[0, i], means[1, i], metadata["x"], metadata["y"], metadata["width"], metadata["height"], scale_x, scale_y,
          metadata["source_sha256"], metadata["output_sha256"], metadata["basis_sha256"]) for i, label in enumerate(bands))))
    buffer = BytesIO()
    np.savez_compressed(buffer, observed_band=field.observed[band], reconstructed_band=field.reconstructed[band],
        relative_residual=field.relative, absolute_residual=field.absolute, observed_norm=field.observed_norm,
        observed_mean=means[0], reconstructed_mean=means[1], metadata_json=np.asarray(json.dumps(metadata, sort_keys=True)))
    outputs.append((figure_id + "/data", "Reconstruction field data", "npz", buffer.getvalue()))
    return tuple(outputs)
