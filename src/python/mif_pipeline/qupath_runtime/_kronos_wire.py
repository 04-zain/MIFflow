"""Neutral closed KRONOS exchange shape; no scientific execution or publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from ._training_codec import (
    QuPathNativeTrainingExchangeError,
    _closed_mapping,
    _array,
    _text,
    _sha256,
    _integer,
    _finite,
)

SCHEMAS = ("qupath-kronos-rtrees/v2", "qupath-kronos-svm/v2")
ENCODER_FIELDS = frozenset(
    {
        "package_directory",
        "package_sha256",
        "worker_command",
        "channel_ids",
        "marker_names",
        "intensity_divisors",
        "pixel_size_um",
        "patch_size",
        "additional_markers",
        "marker_text_model",
        "nuclear_channel",
        "threads",
        "activation_budget_bytes",
        "worker_release",
    }
)
GRAPH_FIELDS = frozenset(
    {
        "hidden_width",
        "epochs",
        "batch_size",
        "learning_rate",
        "weight_decay",
        "random_seed",
        "max_working_bytes",
    }
)


# ==============================================================================
#                                  Kronos wire
# ==============================================================================


def validate_kronos_configuration(value: object) -> Mapping:
    """Validate kronos configuration for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    row = _closed_mapping(
        value,
        frozenset({"configuration_schema", "native_configuration", "encoder", "graph"}),
        "KRONOS configuration",
    )
    schema = row["configuration_schema"]
    if schema not in SCHEMAS:
        raise QuPathNativeTrainingExchangeError("Unsupported KRONOS configuration schema")
    native = row["native_configuration"]
    method = "svm" if schema == SCHEMAS[1] else "rtrees"
    if (
        not isinstance(native, Mapping)
        or native.get("configuration_schema") != f"qupath-pixel-{method}/v1"
    ):
        raise QuPathNativeTrainingExchangeError(
            "KRONOS and native estimator configurations disagree"
        )
    if (
        native.get("maximum_training_samples") != 0
        or native.get("reweight_samples") is not False
        or native.get("boundary_strategy") != "skip"
        or native.get("pca_retained_variance") != 0
        or native.get("pca_normalize") is not False
    ):
        raise QuPathNativeTrainingExchangeError(
            "KRONOS requires unique complete patches without pixel sampling, weighting, boundaries or PCA"
        )
    from mif_pipeline.orchestration.batch_training.qupath_feature_settings import PixelImageFeatures
    try:
        recipe = PixelImageFeatures.from_mapping(native.get("image_features"))
    except ValueError as exc:
        raise QuPathNativeTrainingExchangeError(str(exc)) from exc
    if not recipe.include_intensity or recipe.scales:
        raise QuPathNativeTrainingExchangeError("KRONOS image_features must remain raw-only")
    encoder = _closed_mapping(row["encoder"], ENCODER_FIELDS, "KRONOS encoder")
    for field in ("package_directory",):
        if not (PurePosixPath(_text(encoder[field], field)).is_absolute() or PureWindowsPath(encoder[field]).is_absolute()):
            raise QuPathNativeTrainingExchangeError(
                "KRONOS resource locations must be absolute local paths"
            )
    command = _array(encoder["worker_command"], "KRONOS worker command", nonempty=True)
    if len(command) > 8 or any(type(v) is not str or not v or "\0" in v for v in command) or not (PurePosixPath(command[0]).is_absolute() or PureWindowsPath(command[0]).is_absolute()):
        raise QuPathNativeTrainingExchangeError("KRONOS requires the installed application worker command")
    _sha256(encoder["package_sha256"], "KRONOS package digest")
    from mif_pipeline._release import RELEASE_ID

    if encoder["worker_release"] != RELEASE_ID:
        raise QuPathNativeTrainingExchangeError(
            "The KRONOS worker release differs from this application."
        )
    channels = tuple(_array(encoder["channel_ids"], "KRONOS channels", nonempty=True))
    markers = tuple(_array(encoder["marker_names"], "KRONOS markers", nonempty=True))
    divisors = tuple(_array(encoder["intensity_divisors"], "KRONOS divisors", nonempty=True))
    sizes = tuple(_array(encoder["pixel_size_um"], "KRONOS pixel sizes", nonempty=True))
    count = len(channels)
    if not 1 <= count <= 32 or len(markers) != count or len(divisors) != count or len(sizes) != 2:
        raise QuPathNativeTrainingExchangeError("KRONOS ordered quantitative arrays disagree")
    for values in (channels, markers):
        if any(not isinstance(v, str) or not v.strip() for v in values) or len(set(values)) != len(
            values
        ):
            raise QuPathNativeTrainingExchangeError(
                "KRONOS channel and marker names must be distinct explicit identities"
            )
    for v in (*divisors, *sizes):
        _finite(v, "KRONOS scale", minimum=0.0, positive=True)
    resolution = _finite(
        native.get("resolution_um_per_pixel"), "native input resolution", minimum=0.0, positive=True
    )
    if any(abs(v - resolution) > 1e-9 for v in sizes):
        raise QuPathNativeTrainingExchangeError("KRONOS and native physical sampling disagree")
    nuclear = encoder["nuclear_channel"]
    if nuclear is not None and (type(nuclear) is not int or not 0 <= nuclear < count):
        raise QuPathNativeTrainingExchangeError("KRONOS nuclear-channel index is invalid")
    threads = _integer(encoder["threads"], "KRONOS threads", minimum=1)
    budget = _integer(encoder["activation_budget_bytes"], "KRONOS activation budget", minimum=1)
    if threads > 8:
        raise QuPathNativeTrainingExchangeError("KRONOS thread count exceeds the supported bound")
    from mif_pipeline.compartment_analysis.kronos_encoder import validate_activation_budget

    try:
        validate_activation_budget(count, encoder["patch_size"], budget)
    except ValueError as exc:
        raise QuPathNativeTrainingExchangeError(str(exc)) from exc
    from mif_pipeline.compartment_analysis.kronos_marker_preparation import (
        additional_marker_from_mapping, KronosMarkerTextModel,
    )
    try:
        additions = tuple(additional_marker_from_mapping(item) for item in
                          _array(encoder["additional_markers"], "additional markers"))
        helper = (None if encoder["marker_text_model"] is None else
                  KronosMarkerTextModel.from_mapping(encoder["marker_text_model"]))
        if bool(additions) != (helper is not None):
            raise ValueError("Additional markers require BioLinkBERT; known-only panels need no helper")
    except ValueError as exc:
        raise QuPathNativeTrainingExchangeError(str(exc)) from exc
    graph = _closed_mapping(row["graph"], GRAPH_FIELDS, "KRONOS graph")
    for field, maximum in (("hidden_width", 768), ("epochs", 1000), ("batch_size", 4096)):
        if _integer(graph[field], field, minimum=1) > maximum:
            raise QuPathNativeTrainingExchangeError(
                "KRONOS graph setting exceeds its supported range"
            )
    if _integer(graph["random_seed"], "KRONOS random seed") >= 2**31:
        raise QuPathNativeTrainingExchangeError("KRONOS random seed exceeds its supported range")
    _integer(graph["max_working_bytes"], "KRONOS graph budget", minimum=1)
    if _finite(graph["learning_rate"], "KRONOS learning rate", minimum=0.0, positive=True) > 1:
        raise QuPathNativeTrainingExchangeError("KRONOS learning rate exceeds one")
    if _finite(graph["weight_decay"], "KRONOS weight decay", minimum=0.0) > 1:
        raise QuPathNativeTrainingExchangeError("KRONOS weight decay exceeds one")
    return native
