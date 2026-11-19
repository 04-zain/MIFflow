"""Researcher controls for frozen KRONOS2 features of segmented cells.

Input: the exact processed-image channel inventory and submitted form values.
Output: optional form controls, or a checked feature-source record for the
native supervised RF, SVM or LR configuration. Physical sampling and channel intensity
divisors are explicit researcher choices; the form does not invent either.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math

from mif_pipeline._release import RELEASE_ID
from mif_pipeline.cell_analysis.kronos_features import CELL_EXTRACTION, validate_cell_extraction
from mif_pipeline.compartment_analysis.kronos_encoder import validate_activation_budget
from mif_pipeline.orchestration.batch_training.qupath_kronos_cell import KronosCellFeatureSource
from .interactions import ControlKind, FormControl
from .kronos_setup import (
    inspect_selected_model, patch_size_control, selected_patch_size,
    additional_marker_controls, additional_marker_settings,
)


def _channel_rows(channels):
    if (not isinstance(channels, Sequence) or isinstance(channels, (str, bytes))
            or not channels):
        raise ValueError("KRONOS cell setup needs the current processed-image channel inventory")
    rows = list(channels)
    for row in rows:
        if (not isinstance(row, Mapping)
                or any(type(row.get(key)) is not str or not row[key].strip()
                       or "\x00" in row[key] for key in ("channel_id", "name"))):
            raise ValueError("Each KRONOS input needs its exact channel ID and observed image name")
    if len({row["channel_id"] for row in rows}) != len(rows):
        raise ValueError("The KRONOS image channel inventory contains repeated channel IDs")
    return rows


def cell_kronos_controls(index, channels, *, pixel_size_um=None, nuclear_channel_id=None, intensity_divisor=None):
    """Create cell-only controls and readable labels for exact channel choices.

    Inputs are a supervised-method index, current image-channel rows and optional
    physical sampling read from the participating images. For a group, the caller
    chooses the largest source pixel size on each axis to avoid upsampling. The
    returned controls are optional at the general form level so another method
    can be selected; ``build_cell_kronos_source`` requires every applicable
    scientific choice when a KRONOS cell method is actually submitted.
    """
    from mif_pipeline._defaults import default_section

    rows = _channel_rows(channels)
    if pixel_size_um is not None:
        if not isinstance(pixel_size_um, (list, tuple)) or len(pixel_size_um) != 2:
            raise ValueError("Image sampling requires X and Y pixel sizes")
        pixel_size_um = tuple(_positive(value, "Image pixel size") for value in pixel_size_um)
    defaults = default_section("classifier.kronos")
    prefix = f"supervised_{index}_kronos_"
    labels = {row["channel_id"]: row["name"] for row in rows}
    ids = tuple(labels)
    controls = [
        patch_size_control(prefix + "patch_size"),
        FormControl(prefix + "channel_ids", "Channels to encode",
            ControlKind.OBJECT_SEQUENCE, required=False, choices=ids, value=(),
            help_text="Assign the selected channels to KRONOS markers on the next page."),
        FormControl(prefix + "pixel_x_um", "Sampling resolution X (µm/pixel)",
            ControlKind.NUMBER, required=False, value=None if pixel_size_um is None else pixel_size_um[0],
            help_text="Physical size represented by each sampled patch pixel: width (X) and height (Y). Defaults to the original image pixel size, or the largest pixel size in the group."),
        FormControl(prefix + "pixel_y_um", "Sampling resolution Y (µm/pixel)",
            ControlKind.NUMBER, required=False, value=None if pixel_size_um is None else pixel_size_um[1],
            help_text=""),
        FormControl(prefix + "threads", "Processor threads", ControlKind.INTEGER,
            required=False, value=defaults["threads"],
            help_text="Number of CPU threads available to the frozen encoder, from one to eight."),
        FormControl(prefix + "memory_mib", "Memory for cell features (MiB)", ControlKind.INTEGER,
            required=False, value=defaults["activation_budget_mib"],
            help_text="Allowance for encoder intermediate calculations for one cell. The model and application need additional memory; larger marker panels require more."),
    ]
    for number, row in enumerate(rows):
        controls.extend((
            FormControl(f"{prefix}panel_{number}_marker", row["name"],
                ControlKind.TEXT, required=False, value=row.get("biomarker"),
                help_text=""),
            FormControl(f"{prefix}panel_{number}_divisor", f'{row["name"]}: intensity divisor',
                ControlKind.NUMBER, required=False, value=intensity_divisor,
                help_text=""),
        ))
        controls.extend(additional_marker_controls(prefix + f"panel_{number}", row["name"]))
    nuclear_prefixes = {f"{prefix}panel_{i}" for i, row in enumerate(rows)
                        if row["channel_id"] == nuclear_channel_id}
    controls = [control for control in controls if not any(
        control.name == item + "_marker" or control.name.startswith(item + "_additional_")
        for item in nuclear_prefixes)]
    return controls, {prefix + "channel_ids": dict(labels)}


def _positive(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(label + " must be a finite positive number")
    return float(value)


def build_cell_kronos_source(decoded, index, channels, *, setup=None, nuclear_channel_id=None,
                             extraction=None, saved_encoder=None):
    """Inspect the selected package and preserve exact cell input choices.

    Input: submitted values under ``supervised_{index}_kronos_`` and the same
    offered channel inventory. Output: the closed ``kronos-cell-feature-source/v1``
    mapping embedded in the native classifier. Only selected channel rows need
    marker/divisor values; their explicit submitted order is preserved.
    """
    from mif_pipeline._defaults import default_section

    if not isinstance(decoded, Mapping):
        raise ValueError("KRONOS cell setup requires submitted form values")
    rows = _channel_rows(channels)
    defaults = default_section("classifier.kronos")
    prefix = f"supervised_{index}_kronos_"
    selected = decoded.get(prefix + "channel_ids")
    offered = {row["channel_id"]: (number, row) for number, row in enumerate(rows)}
    if (not isinstance(selected, (list, tuple)) or not 1 <= len(selected) <= 32
            or any(type(channel) is not str or channel not in offered for channel in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError("Select one to 32 distinct offered image channels for KRONOS cells")
    names = [offered[channel][1]["name"] for channel in selected]
    for name in names:
        if sum(row["name"] == name for row in rows) != 1:
            raise ValueError("KRONOS needs an unambiguous exact image channel name: " + name)
    nuclear = selected.index(nuclear_channel_id) if nuclear_channel_id in selected else None
    markers, divisors = [], []
    for channel in selected:
        number, row = offered[channel]
        marker = "DAPI" if channel == nuclear_channel_id else decoded.get(f"{prefix}panel_{number}_marker")
        if not marker and row.get("biomarker") is None:
            raise ValueError(f'Choose the DNA stain in Channels and markers, or choose a KRONOS marker for {row["name"]}')
        if type(marker) is not str or not marker.strip() or "\x00" in marker:
            raise ValueError(f'Choose a marker for {row["name"]}')
        markers.append(marker.strip())
        divisors.append(_positive(decoded.get(f"{prefix}panel_{number}_divisor"),
                                  f'Intensity divisor for {row["name"]} in Advanced settings on KRONOS channel inputs'))
    pixels = (
        _positive(decoded.get(prefix + "pixel_x_um"), "X physical sampling size"),
        _positive(decoded.get(prefix + "pixel_y_um"), "Y physical sampling size"),
    )
    threads = decoded.get(prefix + "threads", defaults["threads"])
    memory = decoded.get(prefix + "memory_mib", defaults["activation_budget_mib"])
    if type(threads) is not int or not 1 <= threads <= 8:
        raise ValueError("KRONOS processor threads must be an integer from one to eight")
    if type(memory) is not int or memory <= 0:
        raise ValueError("KRONOS activation memory must be a positive integer number of MiB")
    budget = memory * 1024**2
    patch_size = selected_patch_size(decoded.get(prefix + "patch_size"))
    validate_activation_budget(len(selected), patch_size, budget)
    selected_setup, additional = additional_marker_settings(
        decoded, prefix.rstrip("_"), [offered[channel] for channel in selected],
        markers, nuclear, setup=setup, saved_encoder=saved_encoder)
    selected_setup, _panel_sha = inspect_selected_model(
        selected_setup, markers, divisors, pixels, nuclear, patch_size=patch_size,
        additional_markers=additional)
    result = {
        "schema": "kronos-cell-feature-source/v1",
        "encoder": {
            "package_directory": selected_setup["package_directory"], "package_sha256": selected_setup["package_sha256"],
            "worker_command": selected_setup["worker_command"], "worker_release": RELEASE_ID,
            "channel_ids": list(selected), "marker_names": markers, "intensity_divisors": divisors,
            "pixel_size_um": list(pixels), "nuclear_channel": nuclear,
            "patch_size": patch_size,
            "additional_markers": [row.to_mapping() for row in additional],
            "marker_text_model": selected_setup["marker_text_model"] if additional else None,
            "threads": threads, "activation_budget_bytes": budget,
        },
        "channel_names": names,
        "extraction": validate_cell_extraction(CELL_EXTRACTION if extraction is None else extraction),
    }
    return KronosCellFeatureSource.from_mapping(result).to_mapping()


__all__ = ["cell_kronos_controls", "build_cell_kronos_source"]
