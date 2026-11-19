"""Local authoring controls for the supported KRONOS tissue-classifier route.

Submission runs in the existing review worker. The domain parser and native readiness check
validate these exact settings again before execution.

The interface keeps editable researcher choices associated with the current image or
analysis request. Values are passed to the stage-specific validator before they become
accepted processing settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
import math
from mif_pipeline._release import RELEASE_ID
from .interactions import FormControl, ControlKind
from mif_pipeline.orchestration.batch_training.qupath_kronos import QuPathKronosConfiguration

# ==============================================================================
#                                  Kronos setup
# ==============================================================================


_PATCH_SIZES = (32, 64, 128, 256)


def tissue_original_sampling_default(context, candidate_id):
    """Offer the coarsest native pixel size when every supported patch fits the native grid."""
    sizes = context.get("original_pixel_size_um_by_entry_id")
    if sizes is None:
        return None
    axes = [sizes[entry] for entry in context["candidate_entry_ids"][candidate_id]]
    spacing = max(x for x, y in axes)
    for x, y in axes:
        if x != y:
            return None
        downsample = spacing / x
        # QuPath's tissue backend requires an integer level-0 footprint for
        # every patch and checks it to an absolute tolerance of 1e-9 pixels.
        for side in _PATCH_SIZES:
            footprint = side * downsample
            if not math.isfinite(footprint) or abs(footprint - round(footprint)) > 1e-9:
                return None
    return spacing


def tissue_original_resolution_caption(context, candidate_id):
    """Describe the selected candidate's level-0 images from its review metadata."""
    sizes = context.get("original_pixel_size_um_by_entry_id")
    if sizes is None:
        return None
    entries = context["candidate_entry_ids"][candidate_id]
    x, y = sizes[entries[0]]
    if all(tuple(sizes[entry]) == (x, y) for entry in entries):
        size = f"{x:.5g}" if x == y else f"{x:.5g} × {y:.5g}"
        caption = f"Original image resolution  ·  {size} µm/pixel"
        tooltip = f"{x} × {y} µm/pixel (width × height)"
    else:
        caption = f"Original image resolution varies across {len(entries)} images."
        details = []
        for entry in entries:
            sample = context["candidate_participants"][entry]["sample_id"]
            x, y = sizes[entry]
            details.append(f"{sample}  ·  {x} × {y} µm/pixel")
        tooltip = "\n".join(details)
    return caption, tooltip


def kronos_controls(index, channels, *, nuclear_channel_id=None, intensity_divisor=None):
    from mif_pipeline._defaults import default_section

    defaults = default_section("classifier.kronos")
    prefix = f"training_{index}_kronos"
    controls = [
        patch_size_control(prefix + "_patch_size"),
        FormControl(
            prefix + "_width",
            "GNN feature dimension",
            ControlKind.INTEGER,
            required=False,
            value=defaults["hidden_width"],
            help_text="Number of learned features calculated for each image patch and passed to RF or SVM.",
        ),
        FormControl(
            prefix + "_epochs",
            "Training passes",
            ControlKind.INTEGER,
            required=False,
            value=defaults["epochs"],
            help_text="Number of times the graph model trains on all labelled training patches.",
        ),
        FormControl(
            prefix + "_batch",
            "Labelled patches per update",
            ControlKind.INTEGER,
            required=False,
            value=defaults["batch_size"],
            help_text="Number of labelled patches used for one update to the graph model.",
        ),
        FormControl(
            prefix + "_learning_rate",
            "Learning rate",
            ControlKind.NUMBER,
            required=False,
            value=defaults["learning_rate"],
            help_text="Controls how much the model parameters change at each training update.",
        ),
        FormControl(
            prefix + "_weight_decay",
            "Weight regularization",
            ControlKind.NUMBER,
            required=False,
            value=defaults["weight_decay"],
            help_text="Strength of the penalty on large fitted weights. Larger values constrain the model more strongly.",
        ),
        FormControl(
            prefix + "_threads",
            "Processor threads",
            ControlKind.INTEGER,
            required=False,
            value=defaults["threads"],
            help_text="Number of CPU threads available to the KRONOS encoder.",
        ),
        FormControl(
            prefix + "_memory_mib",
            "Memory for image features (MiB)",
            ControlKind.INTEGER,
            required=False,
            value=defaults["activation_budget_mib"],
            help_text="Memory reserved for the encoder’s intermediate calculations. The loaded model and the application also require memory.",
        ),
    ]
    for i, row in enumerate(channels):
        title = row.get("name") or row.get("biomarker") or row.get("component") or f"Channel {i+1}"
        controls.extend(
            (
                FormControl(
                    f"{prefix}_panel_{i}_marker",
                    title,
                    ControlKind.TEXT,
                    required=False,
                    value="DAPI" if row["channel_id"] == nuclear_channel_id else row.get("biomarker"),
                    help_text="",
                ),
                FormControl(
                    f"{prefix}_panel_{i}_divisor",
                    f"{title}: intensity divisor",
                    ControlKind.NUMBER,
                    required=False, value=intensity_divisor,
                    help_text="",
                ),
            )
        )
        controls.extend(additional_marker_controls(f"{prefix}_panel_{i}", title))
    nuclear_prefixes = {f"{prefix}_panel_{i}" for i, row in enumerate(channels)
                        if row["channel_id"] == nuclear_channel_id}
    return [control for control in controls if not any(
        control.name == item + "_marker" or control.name.startswith(item + "_additional_")
        for item in nuclear_prefixes)]


def patch_size_control(name):
    """Offer the supported square image sizes while retaining the 256-pixel default."""
    return FormControl(name, "Image patch size (pixels)", ControlKind.CHOICE,
        required=False, choices=tuple(str(size) for size in _PATCH_SIZES), value="256",
        help_text="")


def selected_patch_size(value):
    """Read an offered image size without rounding or substituting a missing choice."""
    if type(value) is not str or value not in tuple(str(size) for size in _PATCH_SIZES):
        raise ValueError("Choose a KRONOS image patch size: 32, 64, 128 or 256 pixels")
    return int(value)


def inspect_selected_model(setup, markers, divisors, pixel_sizes, nuclear, *, patch_size, additional_markers):
    """Use the saved model on this host or its inspected streaming snapshot."""
    from mif_pipeline.compartment_analysis.kronos_panel import KronosPanel
    from mif_pipeline.orchestration.setup.kronos_setup import model_setup_snapshot

    selected = model_setup_snapshot() if setup is None else setup
    if not selected:
        raise ValueError("Select the KRONOS model folder in Software setup on the processing computer, then reopen this form.")
    from mif_pipeline.compartment_analysis.kronos_marker_preparation import KronosMarkerTextModel
    text_model = (KronosMarkerTextModel.from_mapping(selected["marker_text_model"])
                  if additional_markers else None)
    from mif_pipeline.compartment_analysis.kronos_marker_preparation import (
        has_pending_marker_statistics, validate_marker_selection,
    )
    if has_pending_marker_statistics(additional_markers):
        validate_marker_selection(selected["marker_csv"].encode("utf-8"), tuple(markers), nuclear, additional_markers)
        # The encoder settings parser checks numerical sampling constraints; no
        # resolved panel or panel checksum exists until training pixels are read.
        return selected, None
    panel = KronosPanel.from_csv_bytes(selected["marker_csv"].encode("utf-8"),
        marker_names=tuple(markers), intensity_divisors=tuple(divisors),
        pixel_size_um=tuple(pixel_sizes), nuclear_channel=nuclear, patch_size=patch_size,
        additional_markers=additional_markers, marker_text_model=text_model)
    return selected, panel.sha256


def build_kronos_configuration(native, decoded, index, channels, selected_channel_ids, *, setup=None, nuclear_channel_id=None, saved_encoder=None):
    from mif_pipeline._defaults import default_section

    defaults = default_section("classifier.kronos")
    prefix = f"training_{index}_kronos"
    selected = set(selected_channel_ids)
    rows = [(i, row) for i, row in enumerate(channels) if row["channel_id"] in selected]
    if not rows or {row["channel_id"] for i, row in rows} != selected:
        raise ValueError("The KRONOS input channels do not match the selected classifier channels")
    markers = []
    divisors = []
    ids = []
    for i, row in rows:
        marker = "DAPI" if row["channel_id"] == nuclear_channel_id else decoded.get(f"{prefix}_panel_{i}_marker")
        divisor = decoded.get(f"{prefix}_panel_{i}_divisor")
        if not marker and row.get("biomarker") is None:
            raise ValueError(f'Choose the DNA stain in Channels and markers, or choose a KRONOS marker for {row.get("name") or row["channel_id"]}')
        if not isinstance(marker, str) or not marker.strip():
            raise ValueError(f'Choose a KRONOS marker for {row.get("name") or row["channel_id"]}')
        if type(divisor) not in (float, int) or divisor <= 0:
            raise ValueError(
                f'Enter a positive intensity divisor in Advanced settings on KRONOS channel inputs for {row.get("name") or row["channel_id"]}'
            )
        markers.append(marker)
        divisors.append(float(divisor))
        ids.append(row["channel_id"])
    nuclear = ids.index(nuclear_channel_id) if nuclear_channel_id in ids else None
    resolution = native["resolution_um_per_pixel"]
    pixels = (resolution, resolution)
    patch_size = selected_patch_size(decoded.get(prefix + "_patch_size"))
    selected_setup, additional = additional_marker_settings(
        decoded, prefix, rows, markers, nuclear, setup=setup, saved_encoder=saved_encoder)
    selected_setup, _panel_sha = inspect_selected_model(
        selected_setup, markers, divisors, pixels, nuclear, patch_size=patch_size,
        additional_markers=additional)
    method = "svm" if decoded.get(f"training_{index}_model_method") == "kronos_svm" else "rtrees"
    mapping = {
        "configuration_schema": f"qupath-kronos-{method}/v2",
        "native_configuration": native,
        "encoder": {
            "package_directory": selected_setup["package_directory"],
            "package_sha256": selected_setup["package_sha256"],
            "worker_command": selected_setup["worker_command"],
            "worker_release": RELEASE_ID,
            "channel_ids": ids,
            "marker_names": markers,
            "intensity_divisors": divisors,
            "pixel_size_um": list(pixels),
            "patch_size": patch_size,
            "additional_markers": [row.to_mapping() for row in additional],
            "marker_text_model": selected_setup["marker_text_model"] if additional else None,
            "nuclear_channel": nuclear,
            "threads": decoded.get(prefix + "_threads", defaults["threads"]),
            "activation_budget_bytes": decoded.get(
                prefix + "_memory_mib", defaults["activation_budget_mib"]
            )
            * 1024
            * 1024,
        },
        "graph": {
            "hidden_width": decoded.get(prefix + "_width", defaults["hidden_width"]),
            "epochs": decoded.get(prefix + "_epochs", defaults["epochs"]),
            "batch_size": decoded.get(prefix + "_batch", defaults["batch_size"]),
            "learning_rate": decoded.get(prefix + "_learning_rate", defaults["learning_rate"]),
            "weight_decay": decoded.get(prefix + "_weight_decay", defaults["weight_decay"]),
            "random_seed": decoded.get(f"training_{index}_seed", 0),
            "max_working_bytes": defaults["max_working_bytes"],
        },
    }
    return QuPathKronosConfiguration.from_mapping(mapping).to_mapping()

def additional_marker_controls(prefix, title):
    """Offer only novel-marker biology; normalization is fitted from training tissue.

    The renderer shows these fields only for a selected unknown marker. Category
    editors use the processing host's current vocabulary; the shared panel parser
    remains responsible for exact categories and ordered scientific inputs.
    """
    fields = (
        ("marker_full_name", "full marker name", ControlKind.TEXT,
         "Describe the biological protein or marker identity, rather than an image-channel alias."),
        ("compartment", "subcellular location", ControlKind.TEXT,
         "Choose an existing KRONOS compartment for this marker's biological location."),
        ("family", "marker family", ControlKind.TEXT,
         "Choose an existing KRONOS marker family."),
    )
    return [FormControl(prefix + "_additional_" + name, f"{title}: {label}", kind,
                        required=False, help_text=help_text)
            for name, label, kind, help_text in fields]


def additional_marker_settings(decoded, prefix, rows, markers, nuclear, *, setup, saved_encoder=None):
    """Collect only unknown selected markers in their exact submitted channel order."""
    from mif_pipeline.compartment_analysis.kronos_marker_preparation import (
        KronosAdditionalMarker, KronosAdditionalMarkerDefinition, marker_key, marker_vocabulary,
        additional_marker_from_mapping,
    )
    from mif_pipeline.orchestration.setup.kronos_setup import model_setup_snapshot

    selected = model_setup_snapshot() if setup is None else setup
    if not selected:
        raise ValueError("Select the KRONOS model folder in Software setup, then reopen this form")
    vocabulary = marker_vocabulary(selected["marker_csv"].encode("utf-8"))
    known = vocabulary["markers"]
    additional = []
    for position, ((number, row), marker) in enumerate(zip(rows, markers)):
        key = "dapi" if position == nuclear else marker_key(marker)
        if key in known:
            continue
        field_prefix = f"{prefix}_panel_{number}_additional_"
        values = {name: decoded.get(field_prefix + name)
                  for name in ("marker_full_name", "compartment", "family")}
        values["family_desc"] = vocabulary["family_descriptions"].get(values["family"])
        try:
            definition = KronosAdditionalMarkerDefinition(marker_name=marker, **values)
            retained = None
            if saved_encoder is not None and row["channel_id"] in saved_encoder["channel_ids"]:
                old_position = saved_encoder["channel_ids"].index(row["channel_id"])
                old_divisor = saved_encoder["intensity_divisors"][old_position]
                if (marker_key(saved_encoder["marker_names"][old_position]) == marker_key(marker)
                        and old_divisor == decoded.get(f"{prefix}_panel_{number}_divisor")):
                    for old in saved_encoder["additional_markers"]:
                        checked = additional_marker_from_mapping(old)
                        if (type(checked) is KronosAdditionalMarker and marker_key(checked.marker_name) == key and
                                all(getattr(checked, field) == value for field, value in definition.to_mapping().items()
                                    if field not in {"family_desc", "marker_name"})):
                            retained = checked
                            break
            additional.append(retained if retained is not None else definition)
        except ValueError as exc:
            title = row.get("name") or row["channel_id"]
            raise ValueError(f"Complete the new-marker metadata for {title}: {exc}") from exc
    if additional and selected.get("marker_text_model") is None:
        raise ValueError("New KRONOS markers require the marker-description model. In Software, open KRONOS and install or select BioLinkBERT-large, then reopen this form")
    return selected, tuple(additional)
