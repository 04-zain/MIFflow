"""Presentation of explicit settings for the existing segmentation stage.

The interface keeps editable researcher choices associated with the current image or
analysis request. Values are passed to the stage-specific validator before they become
accepted processing settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path, PurePosixPath

from mif_pipeline.gui.interactions import ControlKind, FormControl, FormModel
from mif_pipeline.pipeline_core import InteractionReason, StageResult, StageStatus
from mif_pipeline.pipeline_core.interactions import validate_interaction_response
from mif_pipeline.qupath_runtime.payload_lock import MODEL_VERSION

_FIELDS = frozenset(
    {
        "tile_size_px",
        "padding_px",
        "workers",
        "scope", "method", "channel_1", "channel_2", "channel_3", "pixel_size_um",
        "flow_threshold", "cellprob_threshold", "probability_threshold", "cell_expansion_um",
        "limit_cell_expansion", "cell_constrain_scale",
    }
)
_NUMERIC_FIELDS = ("tile_size_px", "padding_px", "workers")

_CHOICE_LABELS = {
    "method": {"instanseg": "InstanSeg — nuclei and cells", "cellpose": "Cellpose-SAM — cell boundaries", "stardist": "StarDist — nuclear outlines"},
    "scope": {
        "accepted_tissue": "Selected analysis regions",
        "configured_union": "Included primary tissue regions",
    },
}


# ==============================================================================
#                               Segmentation setup
# ==============================================================================


def build_segmentation_setup_form(
    result: StageResult,
    *,
    model_directory: Path | None = None,
) -> FormModel:
    """Show segmentation choices using the model checked in Software setup.

    We retain the checked model folder and Computing processor choice with the
    form so submission records the settings used for this segmentation. Model
    inspection remains in Software setup.
    """
    if model_directory is not None and not isinstance(model_directory, Path):
        raise TypeError("model_directory must be a Path or None")
    if (
        type(result) is not StageResult
        or result.status is not StageStatus.NEEDS_INTERACTION
        or result.interaction_reason is not InteractionReason.SEGMENTATION_SETUP_REQUIRED
    ):
        raise ValueError("segmentation setup requires its stage interaction")
    context = result.resume_context or {}
    if (
        set(context) != ({"schema_version", "checkpoint_id", "section_id"}
            | ({"channels"} if "channels" in context else set())
            | ({"nuclear_channel_id"} if "nuclear_channel_id" in context else set())
            | ({"primary_classification_skipped"} if context.get("primary_classification_skipped") is True else set()))
        or context.get("schema_version") != "segmentation-setup-interaction/v1"
        or context.get("checkpoint_id") != result.checkpoint_id
        or type(context.get("section_id")) is not str
        or not context["section_id"]
        or context["section_id"] != context["section_id"].strip()
    ):
        raise ValueError("segmentation setup owner context is invalid")
    return FormModel(
        form_id="segmentation_setup",
        title="Cell segmentation setup",
        message="Choose which outlines to detect for cell measurements and phenotyping.",
        allowed_actions=result.allowed_actions,
        controls=_segmentation_controls(primary_skipped=context.get("primary_classification_skipped", False), channels=context.get("channels", ())),
        context={
            "adapter": "segmentation_setup",
            "stage_result": result.to_mapping(),
            "model_path": str(model_directory) if model_directory is not None else None,
            "device": _computing_device(),
            "choice_labels": _choice_labels(context.get("channels", ())),
            "nuclear_channel_index": _nuclear_channel_index(context.get("channels", ()), context.get("nuclear_channel_id")),
        },
    )


def normalized_segmentation_model_path(path: object) -> str:
    if type(path) is not str or not path.strip() or "\0" in path:
        raise ValueError("model_path requires a local model directory")
    return os.path.abspath(os.path.expanduser(path.strip()))


def _explicit_integer(values: Mapping[str, object], name: str) -> int:
    value = values.get(name)
    if isinstance(value, bool) or value is None or (isinstance(value, str) and not value.strip()):
        if name == "workers":
            raise ValueError("workers must be one of the offered choices")
        raise ValueError(f"{name} is required")
    if type(value) is str:
        try:
            value = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    if name == "workers" and value not in (1, 2):
        raise ValueError("workers must be one of the offered choices")
    return value


def submit_segmentation_setup_form(
    form: FormModel,
    action: str,
    raw_values: Mapping[str, object],
) -> dict[str, object]:
    """Bind explicit UI values through the shared scientific response validator."""
    if (
        type(form) is not FormModel
        or form.context.get("adapter") != "segmentation_setup"
        or action not in form.allowed_actions
    ):
        raise ValueError("segmentation setup action was not offered")
    if not isinstance(raw_values, Mapping) or set(raw_values) != _FIELDS:
        raise ValueError("segmentation setup requires exactly its displayed fields")
    numeric = {name: _explicit_integer(raw_values, name) for name in _NUMERIC_FIELDS}
    scope = raw_values["scope"]
    if type(scope) is not str or scope not in {"accepted_tissue", "configured_union"}:
        raise ValueError("scope requires an offered area")
    settings = _selected_settings(form, raw_values)
    result = StageResult.from_mapping(form.context["stage_result"])
    response = {"segmentation": settings}
    validate_interaction_response(result, action, response)
    return response


def _computing_device() -> str:
    """Translate the Computing processor preference to InstanSeg's device name."""
    return {"cuda": "gpu"}.get(
        os.environ.get("MIF_COMPUTE_DEVICE", "auto"),
        os.environ.get("MIF_COMPUTE_DEVICE", "auto"),
    )


def _installed_model_path(form: FormModel) -> str:
    """Use the processing machine's model folder without changing remote paths.

    Local folders come from the Software setup check. Cluster review pages
    supply a POSIX path, which must remain unchanged on a Windows desktop.
    Missing installations direct the researcher to Software setup.
    """
    path = form.context.get("model_path")
    if path is None:
        if form.context.get("remote_execution"):
            raise ValueError("Install InstanSeg in cluster Software setup, then refresh this review.")
        raise ValueError("Complete Software setup → Cell segmentation, then reopen this step.")
    if form.context.get("remote_execution"):
        if (
            not isinstance(path, str)
            or not PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            or "\0" in path
        ):
            raise ValueError("Refresh this review to load the cluster segmentation model folder.")
        return path
    return normalized_segmentation_model_path(path)


def _segmentation_controls(*, settings=None, primary_skipped=False, channels=()):
    """Show the segmentation area and optional tile-processing settings."""
    from mif_pipeline._defaults import default_section
    defaults = default_section("segmentation")
    method_controls = _method_controls(settings, channels)
    return (method_controls[0],
            FormControl(
                "scope",
                "Area to segment",
                ControlKind.CHOICE,
                choices=("accepted_tissue",) if primary_skipped else ("accepted_tissue", "configured_union"),
                value=("accepted_tissue" if primary_skipped else
                       settings.scope if settings is not None else defaults["scope"]),
                help_text=("Segment the analysis regions selected for this section." if primary_skipped else
                    "Segment each analysis region in full, or use only its included tissue classes. Areas labelled Ignore are excluded when restricting by class."),
            ),
            *method_controls[1:],
            FormControl(
                "tile_size_px",
                "Tile size (pixels)",
                ControlKind.INTEGER,
                value=settings.tile_size_px if settings is not None else defaults["tile_size_px"],
                help_text="Width and height in model-input pixels, including padding on both sides. Larger tiles require more working memory.",
            ),
            FormControl(
                "padding_px",
                "Padding (pixels)",
                ControlKind.INTEGER,
                value=settings.padding_px if settings is not None else defaults["padding_px"],
                help_text="Padding lets the model see neighbouring pixels when outlining a cell at the edge of a tile.",
            ),
            FormControl(
                "workers",
                "Simultaneous tiles",
                ControlKind.CHOICE,
                choices=("1", "2"),
                value=str(settings.workers if settings is not None else defaults["workers"]),
                help_text="With two tiles, the next tile can be read during prediction on the current tile. This uses more working memory.",
            ),

        )


def build_segmentation_options_form(settings, *, channels=(), nuclear_channel_id=None) -> FormModel:
    """Open retained options without manufacturing an interaction checkpoint."""
    from mif_pipeline.pipeline_core.config import SegmentationConfig
    if type(settings) is not SegmentationConfig:
        raise TypeError("segmentation options require typed saved settings")
    message = "Review this section's saved options. Preview shows which downstream steps must run again."
    if settings.workers not in (1, 2):
        message = (
            f"Change the saved value of {settings.workers} simultaneous tiles to 1 or 2 "
            "in Advanced settings, then select Preview to review the affected results."
        )
    return FormModel(form_id="segmentation_options", title="Segmentation options",
        message=message,
        allowed_actions=("preview",), controls=_segmentation_controls(settings=settings, channels=channels),
        context={"adapter": "segmentation_options", "model_version": settings.model_version,
                 "choice_labels": _choice_labels(channels), "model_path": str(settings.model_path),
                 "method": settings.method,
                 "nuclear_channel_index": _nuclear_channel_index(channels, nuclear_channel_id),
                 "device": _computing_device()})


def submit_segmentation_options_form(form, action, raw_values):
    from mif_pipeline.pipeline_core.config import SegmentationConfig
    if action != "preview" or form.context.get("adapter") != "segmentation_options":
        raise ValueError("Preview the current segmentation options.")
    if set(raw_values) != _FIELDS:
        raise ValueError("segmentation options require exactly their displayed fields")
    values = SegmentationConfig.from_mapping(_selected_settings(form, raw_values), field="segmentation").to_mapping()
    return {"segmentation": values}


def _nuclear_channel_index(channels, channel_id):
    rows = tuple(item.to_mapping() if hasattr(item, "to_mapping") else item for item in channels)
    matches = [index for index, item in enumerate(rows) if item["channel_id"] == channel_id]
    return str(matches[0]) if len(matches) == 1 else None


def _choice_labels(channels):
    rows = tuple(item.to_mapping() if hasattr(item, "to_mapping") else item for item in channels)
    labels = {str(index): f"{index + 1}. {item.get('name') or item['channel_id']}" for index, item in enumerate(rows)}
    return {**_CHOICE_LABELS,
            "channel_1": {"": "Select a channel", **labels},
            "channel_2": {"": "Not used", **labels},
            "channel_3": {"": "Not used", **labels}}


def _method_controls(settings, channels):
    parameters = {} if settings is None or settings.parameters is None else settings.parameters
    selected = list(parameters.get("channels", ()))
    options = ("", *(str(index) for index in range(len(channels))))
    controls = [FormControl("method", "Segmentation method", ControlKind.CHOICE,
        choices=("instanseg", "cellpose", "stardist"), value=settings.method if settings else "instanseg",
        help_text="Detects nuclei and cell outlines.")]
    for index, label in enumerate(("Primary input channel", "Second input channel (optional)", "Third input channel (optional)")):
        value = str(selected[index]) if index < len(selected) else ""
        controls.append(FormControl(f"channel_{index + 1}", label, ControlKind.CHOICE if channels else ControlKind.TEXT,
            choices=options if channels else (), value=value, required=index == 0,
            help_text=("Choose the channel used for segmentation." if index == 0 else
                       "Add another marker or nuclear stain.")))
    controls.extend((
        FormControl("pixel_size_um", "Processing pixel size (µm)", ControlKind.NUMBER,
                    value=parameters.get("pixel_size_um", 0.5),
                    help_text="Width and height of one pixel supplied to the model. The image is resampled to this size before prediction."),
        FormControl("cell_expansion_um", "Estimated cell expansion (µm)", ControlKind.NUMBER,
                    value=parameters.get("cell_expansion_um", 0.0),
                    help_text="Distance to expand each nucleus when estimating a cell region. Set 0 for nuclear outlines only."),
        FormControl("limit_cell_expansion", "Limit expansion relative to nucleus size", ControlKind.CHECKBOX,
                    value="cell_constrain_scale" in parameters),
        FormControl("cell_constrain_scale", "Expansion scale factor", ControlKind.NUMBER,
                    value=parameters.get("cell_constrain_scale", 1.5),
                    help_text="Scale applied to the shape enclosing the nucleus. A value of 1.5 enlarges it by 50% in both image directions and limits the estimated cell to that area."),
        FormControl("cellprob_threshold", "Cell score threshold", ControlKind.NUMBER,
                    value=parameters.get("cellprob_threshold", 0.0),
                    help_text="Minimum model score for including a pixel when forming cell outlines. Lower values include more of the surrounding pixels."),
        FormControl("flow_threshold", "Flow error threshold", ControlKind.NUMBER,
                    value=parameters.get("flow_threshold", 0.4),
                    help_text="Maximum allowed disagreement between image-predicted and outline-derived pixel directions. Cellpose first follows directions predicted from the image to form candidate cell outlines. It then calculates directions from each candidate's enclosed shape and compares them with the image predictions. Candidates whose mean squared difference exceeds this threshold are discarded."),
        FormControl("probability_threshold", "Nuclear detection threshold", ControlKind.NUMBER,
                    value=parameters.get("probability_threshold", 0.5),
                    help_text="Minimum object probability predicted by StarDist for considering a nuclear outline. Lower values admit more candidate outlines."),
    ))
    return tuple(controls)


def _selected_settings(form, raw_values):
    from mif_pipeline.orchestration.setup.alternative_segmentation_setup import default_detector_directory, detector_release
    method = raw_values["method"]
    if method not in {"instanseg", "cellpose", "stardist"}:
        raise ValueError("Choose a segmentation method")
    same_method = form.context.get("method", "instanseg") == method
    if same_method and form.context.get("model_path"):
        model_path = form.context["model_path"]
    elif form.context.get("remote_execution"):
        model_path = form.context.get("model_directories", {}).get(method)
        if model_path is None:
            raise ValueError(f"Install {method.title()} on the cluster, then refresh this review.")
    elif method == "instanseg":
        if same_method:
            model_path = _installed_model_path(form)
        else:
            import sys
            from mif_pipeline.orchestration.setup.instanseg_setup import default_instanseg_model_target
            from mif_pipeline.qupath_runtime.payload_lock import MODEL_NAME
            root = Path(sys._MEIPASS) / "qupath-assets" if getattr(sys, "frozen", False) else default_instanseg_model_target("win-x86_64" if sys.platform == "win32" else "linux-x86_64")
            model_path = str(root / "model" / MODEL_NAME)
            if not Path(model_path).is_dir():
                raise ValueError("Complete InstanSeg Software setup, then reopen this step.")
    else:
        model_path = str(default_detector_directory(method))
    settings = {"model_path": model_path,
                "model_version": (form.context.get("model_version", MODEL_VERSION) if same_method else MODEL_VERSION) if method == "instanseg" else detector_release(method)["version"],
                "device": "cpu" if method == "stardist" else form.context["device"],
                **{name: _explicit_integer(raw_values, name) for name in _NUMERIC_FIELDS},
                "scope": raw_values["scope"]}
    if method != "instanseg":
        channels = []
        for name in (("channel_1",) if method == "stardist" else ("channel_1", "channel_2", "channel_3")):
            value = raw_values.get(name)
            if value in (None, ""):
                if name == "channel_1": raise ValueError("Select the primary input channel")
                continue
            channels.append(int(value))
        names = ("flow_threshold", "cellprob_threshold") if method == "cellpose" else ("probability_threshold", "cell_expansion_um")
        parameters = {name: float(raw_values[name]) for name in ("pixel_size_um", *names)}
        if method == "stardist" and parameters["cell_expansion_um"] > 0 and raw_values["limit_cell_expansion"] is True:
            parameters["cell_constrain_scale"] = float(raw_values["cell_constrain_scale"])
        parameters["channels"] = channels
        settings.update(method=method, parameters=parameters, workers=1)
    return settings
