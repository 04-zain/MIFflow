"""

Prepares the detector, image resolution and tissue-region settings for saving.

We select a detector method or a saved detector. Image detail chooses its
reference channel, native resolution and RF/SVM features before annotation; the final page
sets the minimum area retained as a separate tissue region. The submitted
parameters combine those choices with the region-comparison settings already
recorded for this step.

For an imported detector, we read its file once to record the absolute path,
size and checksum. The save response contains the selected parameters and
that file description. Processing checks the same choices against its pending
request before importing the detector and recording the completed decision.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import math
import os
from pathlib import Path
import stat

from mif_pipeline.gui.interactions import ControlKind, FormControl, FormModel
from mif_pipeline.pipeline_core import InteractionReason, StageResult, StageStatus
from mif_pipeline.pipeline_core.interactions import (
    resolve_resume_envelope,
    validate_interaction_response,
)
from mif_pipeline.tissue_detection.setup import (
    MAX_TISSUE_CLASSIFIER_BYTES,
    tissue_setup_authoring_seed,
)



# ==============================================================================
#                                  Tissue setup
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ObservedTissueClassifierSource:
    """

    Records the local detector file selected for import.

    Attributes:
        path: Absolute path used to read the detector.
        sha256: Checksum of its file contents.
        byte_count: Number of bytes read, used with the checksum to check the import.
    """

    path: str
    sha256: str
    byte_count: int

    def to_mapping(self) -> dict[str, object]:
        """

        Creates the detector-file description included in an import response.

        Returns:
            The selected path, file checksum and byte count.
        """

        return {"path": self.path, "sha256": self.sha256, "byte_count": self.byte_count}


def normalized_tissue_source_path(path: str) -> str:
    """

    Expands a home-directory shortcut and converts the detector path to an absolute path.

    Args:
        path: Path entered or selected in the detector-file control.

    Returns:
        The absolute path with surrounding input whitespace removed.

    Raises:
        ValueError: The path is blank, is not text or contains a null character.
    """

    if type(path) is not str or not path.strip() or "\0" in path:
        raise ValueError("classifier source path is required")
    return os.path.abspath(os.path.expanduser(path.strip()))


def observe_tissue_classifier_source(path: str) -> ObservedTissueClassifierSource:
    """

    Reads the selected detector and records its size and checksum for the import step.

    Open the file once and hash it in blocks of up to 1 MiB. Its size must be positive
    and no greater than 64 MiB. Compare the size and modification time before and after
    reading so a detector being rewritten during selection is rejected.

    Args:
        path: Local detector file selected by the researcher.

    Returns:
        An ObservedTissueClassifierSource containing the checked path, checksum and size.

    Raises:
        ValueError: The selection is not a regular file, exceeds the size limit,
            or changes during the read.
        OSError: The selected file cannot be opened or read.
    """

    normalized = normalized_tissue_source_path(path)
    if not stat.S_ISREG(os.lstat(normalized).st_mode):
        raise ValueError("classifier source must be a regular readable file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(normalized, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_TISSUE_CLASSIFIER_BYTES:
            raise ValueError("classifier source must be a nonempty regular file of at most 64 MiB")
        digest = hashlib.sha256()
        count = 0
        with os.fdopen(fd, "rb", closefd=False) as stream:
            while chunk := stream.read(min(1024 * 1024, MAX_TISSUE_CLASSIFIER_BYTES + 1 - count)):
                count += len(chunk)
                if count > MAX_TISSUE_CLASSIFIER_BYTES:
                    raise ValueError("classifier source exceeds 64 MiB")
                digest.update(chunk)
        after = os.fstat(fd)
        if (
            count != before.st_size
            or count != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ValueError("classifier source changed during observation")
    finally:
        os.close(fd)
    return ObservedTissueClassifierSource(normalized, digest.hexdigest(), count)


# ==============================================================================
#                  Building the detector and tissue-region form
# ==============================================================================


def build_tissue_setup_form(
    result: StageResult,
    *,
    validation_domain_id: str,
) -> FormModel:
    """

    Builds the three tissue-setup pages from the current image and saved detector list.

    The pending request supplies the actual image channels and native pyramid
    levels. We use their identifiers to populate choices and their physical pixel
    sizes to label the resolution selector. This keeps the selected channel and
    level tied to the image on which the detector will run.

    Initial numerical settings come from tissue_detection.json. Existing step
    settings are retained when supplied, and a returned detector-editing result
    can fill its channel and resolution in the displayed form.

    Args:
        result: Pending tissue-setup request with the image channels, native levels
            and detector files currently available in the project.
        validation_domain_id: Processing-group identifier recorded with the detector settings.

    Returns:
        FormModel containing detector selection, image-detail and minimum-area fields.

    Raises:
        ValueError: The request does not describe the current tissue-setup step or
            lacks the information needed to build its choices.
    """

    if (
        type(result) is not StageResult
        or result.status is not StageStatus.NEEDS_INTERACTION
        or result.interaction_reason is not InteractionReason.TISSUE_DETECTOR_SETUP_REQUIRED
    ):
        raise ValueError("tissue setup requires its native interaction")
    context = resolve_resume_envelope(result.resume_context or {}).service_context
    if context.get("schema_version") != "tissue-detector-setup-interaction/v1":
        raise ValueError("tissue setup interaction schema is unsupported")
    seed = tissue_setup_authoring_seed(validation_domain_id=validation_domain_id)
    form = tissue_setup_controls(context, seed, allowed_actions=result.allowed_actions)
    return replace(form, context={**form.context, "stage_result": result.to_mapping(),
                                  "resume_context": result.resume_context})


def tissue_setup_controls(context, seed, *, allowed_actions, initial_native_resolution_index=None):
    """Build controls from image presentation data, without creating review authority."""
    resources = tuple(row["classifier_name"] for row in context["available_classifiers"])
    channels = tuple(row["channel_id"] for row in context["channels"])
    indices = tuple(
        str(row["native_resolution_index"]) for row in context["native_pyramid_inventory"]
    )
    inventory = context["native_pyramid_inventory"]
    saved_index = (seed.get("native_resolution_index") if initial_native_resolution_index is None
                   else initial_native_resolution_index)
    default_index = str(saved_index) if str(saved_index) in indices else str(min(
        inventory,
        key=lambda row: max(
            abs(math.log(row[axis] / seed["resolution_um_per_px"]))
            for axis in ("x_um_per_px", "y_um_per_px")
        ),
    )["native_resolution_index"])
    from mif_pipeline._defaults import default_section

    feature_defaults = default_section("tissue_detection.editor")["image_features_default"]
    native_level = next(row for row in inventory if str(row["native_resolution_index"]) == default_index)
    pixel_size = (native_level["x_um_per_px"] + native_level["y_um_per_px"]) / 2
    image_features = {
        "include_intensity": feature_defaults["include_intensity"],
        "scales": [{"sigma_um": row["sigma_px"] * pixel_size, "filters": row["filters"]}
                   for row in feature_defaults["scales"]],
    }
    labels = {
        "detector_source": {
            "threshold": "Pixel threshold — QuPath",
            "rf": "Random forest — annotated examples",
            "svm": "Support vector machine — annotated examples",
            "saved": "Saved detector",
            "import": "Browse detector…",
        },
        "native_level_selection_mode": {
            "prompt": "Review available image resolutions",
            "automatic_target": "Match a target resolution within its allowed difference",
            "configured_index": "Use a specific image resolution",
        },
        "reference_channel_id": {
            row["channel_id"]: f"{i+1}. {row["name"] or "Unnamed channel"}"
            for i, row in enumerate(context["channels"])
        },
        "native_resolution_index": {
            str(row["native_resolution_index"]): f"Level {row["native_resolution_index"]}"
            + (
                f" · {row["x_um_per_px"]:.3g} × {row["y_um_per_px"]:.3g} µm/px"
                if "x_um_per_px" in row and "y_um_per_px" in row
                else ""
            )
            for row in context["native_pyramid_inventory"]
        },
    }
    controls = (
        FormControl(
            "detector_source",
            "Detector source",
            ControlKind.CHOICE,
            choices=("threshold", "rf", "svm", "saved", "import"),
            value="threshold",
            help_text="Set an intensity threshold or train a Tissue/Background classifier from annotated regions.",
        ),
        FormControl(
            "saved_classifier_name",
            "Saved detector",
            ControlKind.CHOICE,
            choices=resources,
            value=None,
            help_text="Choose a tissue detector already saved in this project.",
        ),
        FormControl(
            "source_path",
            "Detector file",
            ControlKind.LOCAL_FILE,
            value=None,
            help_text="Browse to the saved detector to use for this analysis.",
        ),
        FormControl(
            "import_classifier_name",
            "Save as",
            ControlKind.TEXT,
            value=None,
            help_text="Name the detector in this project.",
        ),
        FormControl(
            "reference_channel_id",
            "Channel for tissue detection",
            ControlKind.CHOICE,
            choices=channels,
            value=None,
            help_text="",
        ),
        FormControl(
            "native_level_selection_mode",
            "Image resolution selection",
            ControlKind.CHOICE,
            choices=("prompt", "automatic_target", "configured_index"),
            value="configured_index",
            help_text="Choose a stored image resolution directly or match it to a target pixel size.",
        ),
        FormControl(
            "native_resolution_index",
            "Image resolution",
            ControlKind.CHOICE,
            choices=indices,
            value=default_index,
            help_text="Smaller pixels retain finer tissue boundaries. Larger pixels reduce memory use and processing time.",
        ),
        FormControl(
            "resolution_um_per_px",
            "Target resolution (µm/pixel)",
            ControlKind.NUMBER,
            value=seed["resolution_um_per_px"],
            help_text="Choose the pixel size you want for detection. The tolerance determines which stored image resolutions can be used.",
        ),
        FormControl(
            "native_level_match_tolerance_fraction",
            "Allowed difference from target",
            ControlKind.NUMBER,
            value=seed["native_level_match_tolerance_fraction"],
            help_text="At a target of 8 µm/pixel, a tolerance of 0.05 accepts a stored resolution if both pixel dimensions fall between 7.6 and 8.4 µm.",
        ),
        FormControl(
            "allow_level0_selection",
            "Allow full resolution among the available choices",
            ControlKind.CHECKBOX,
            value=seed["allow_level0_selection"],
            help_text="Include the original full-resolution view among the available choices. This retains the finest detail and increases memory use and processing time.",
        ),
        FormControl(
            "tissue_image_features",
            "Image features",
            ControlKind.JSON_OBJECT,
            value=image_features,
            help_text="Inspect the image filters used by the random forest or SVM detector.",
        ),
        FormControl(
            "minimum_object_area_um2",
            "Smallest tissue region to retain (µm²)",
            ControlKind.NUMBER,
            value=seed["minimum_object_area_um2"],
            help_text="Tissue fragments smaller than this area are excluded from the detected sections.",
        ),
    )
    return FormModel(
        form_id="tissue_detector_setup",
        title="Tissue detector setup",
        message="Separate tissue from background with a threshold or a trained pixel classifier. Review the live preview in QuPath, then save the detector for this analysis.",
        allowed_actions=allowed_actions,
        controls=controls,
        context={
            "adapter": "tissue_setup",
            "seed": seed,
            "choice_labels": labels,
            "authoring_defaults_version": "tissue-setup-starts/v1",
            "tissue_image_context": context,
            "initial_native_resolution_index": default_index,
        },
        open_qupath_path=context["project_path"],
    )


# ==============================================================================
#                       Decoding the selected form values
# ==============================================================================


def _required_number(values: Mapping[str, object], name: str) -> float:
    """

    Reads a required numerical field and rejects blank or nonfinite values.

    Args:
        values: Form values keyed by control name.
        name: Name of the numerical control to read.

    Returns:
        The entered value as a finite float.

    Raises:
        ValueError: The field is missing, blank, boolean or cannot represent a finite number.
    """

    raw = values.get(name)
    if isinstance(raw, bool) or raw is None or (isinstance(raw, str) and not raw.strip()):
        raise ValueError(f"{name} is required")
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def decode_tissue_setup_values(
    form: FormModel,
    action: str,
    raw_values: Mapping[str, object],
) -> dict[str, object]:
    """

    Checks the completed tissue settings and prepares the values sent by Save and Next.

    We read the selected detector, reference channel and stored image level.
    Selecting level zero explicitly enables full-resolution processing. An
    unchanged saved level preserves the accepted resolution policy and physical
    matching settings; a changed level uses its recorded physical pixel size.

    Imported detector names receive a .json suffix when it is omitted; selected
    saved-detector names retain their offered identity. The response contains
    parameters and, for an imported detector, its checked file description. The processing-side validator adds the accepted decision to
    its original request when saving. Keeping the submitted choices separate from
    that completed description prevents repeated validation from rejecting valid
    inputs as unexpected fields.

    Args:
        form: Current tissue form built from the pending image request.
        action: Offered detector-selection, import or refresh action.
        raw_values: Current editor values. Imported files additionally carry the
            completed path, size and checksum check under __observed_source.

    Returns:
        Parameters for a saved detector, parameters plus source for an import,
        or an empty dictionary when refreshing the available detector list.

    Raises:
        ValueError: An active field is missing, an image choice is not available,
            or the detector file has not completed its current check.
    """

    if form.context.get("adapter") != "tissue_setup" or action not in form.allowed_actions:
        raise ValueError("tissue setup action was not offered")
    if not isinstance(raw_values, Mapping):
        raise ValueError("tissue setup values must be a mapping")
    if action == "refresh_tissue_detectors":
        result = StageResult.from_mapping(form.context["stage_result"])
        validate_interaction_response(result, action, {})
        return {}
    mode = raw_values.get("detector_source")
    if mode not in {"saved", "import"}:
        raise ValueError("detector_source requires a choice")
    selecting = action == "select_tissue_detector"
    if selecting != (mode == "saved"):
        raise ValueError("detector source disagrees with selected action")
    controls = {item.name: item for item in form.controls}
    classifier_field = "saved_classifier_name" if selecting else "import_classifier_name"
    name = raw_values.get(classifier_field)
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{classifier_field} is required")
    if selecting and name not in controls[classifier_field].choices:
        raise ValueError("selected classifier was not offered")
    if not selecting:
        name = name.strip()
        if not name.endswith(".json"):
            name += ".json"
    channel_id = raw_values.get("reference_channel_id")
    if channel_id not in controls["reference_channel_id"].choices:
        raise ValueError("reference_channel_id requires an offered channel")
    raw_index = raw_values.get("native_resolution_index")
    if raw_index not in controls["native_resolution_index"].choices:
        raise ValueError("native_resolution_index: Choose an available image resolution.")
    # Keep accepted batch matching when the displayed resolution is unchanged.
    # Changing this explicit selector requests a fixed level using the existing wire policy.
    seed = form.context["seed"]
    preserve_policy = (form.context.get("saved_tissue_settings")
                       and raw_index == form.context["initial_native_resolution_index"])
    level_mode = seed["native_level_selection_mode"] if preserve_policy else "configured_index"
    index = int(raw_index) if level_mode == "configured_index" else None
    allow_level0 = seed["allow_level0_selection"] if preserve_policy else index == 0

    # Carry forward the recorded region-matching settings when submitting the visible form.
    seed = dict(form.context["seed"])
    seed.update(
        classifier_name=name.strip(),
        reference_channel_id=channel_id,
        reference_channel_selection_source=(seed["reference_channel_selection_source"]
            if channel_id == seed.get("reference_channel_id") else "user_override"),
        native_level_selection_mode=level_mode,
        native_resolution_index=index,
        allow_level0_selection=allow_level0,
        matching=dict(seed["matching"]),
    )
    seed["minimum_object_area_um2"] = _required_number(raw_values, "minimum_object_area_um2")

    # Decode target/tolerance only when that route is selected. A fixed
    # native level already has its own recorded physical pixel dimensions.
    if level_mode == "automatic_target":
        seed.update(
            {
                key: _required_number(raw_values, key)
                for key in ("resolution_um_per_px", "native_level_match_tolerance_fraction")
            }
        )
    elif level_mode == "configured_index" and not preserve_policy:
        context = form.context["tissue_image_context"]
        row = next(
            item
            for item in context["native_pyramid_inventory"]
            if item["native_resolution_index"] == index
        )
        if "x_um_per_px" in row:
            seed["resolution_um_per_px"] = row["x_um_per_px"]
    from mif_pipeline.pipeline_core.config import TissueDetectionParameters
    from mif_pipeline.pipeline_core.errors import ProjectPreparationError
    from mif_pipeline.tissue_detection.resolution import permitted_tissue_native_resolution_indices

    parameters = TissueDetectionParameters.from_mapping(seed, field="tissue_detection")
    context = form.context["tissue_image_context"]
    try:
        permitted_tissue_native_resolution_indices(parameters, context["native_pyramid_inventory"])
    except ProjectPreparationError as error:
        field = (
            "resolution_um_per_px"
            if level_mode == "automatic_target"
            else (
                "native_resolution_index"
                if level_mode == "configured_index"
                else "allow_level0_selection"
            )
        )
        explanation = (
            "Choose an available image resolution or adjust the target and its allowed difference."
            if level_mode == "automatic_target"
            else "Choose an available image resolution."
        )
        raise ValueError(field + ": " + explanation) from error
    response: dict[str, object] = {"parameters": seed}

    # Import consumes the file check completed by the separate observation
    # job. A changed path requires a fresh observation before submission.
    if not selecting:
        source = raw_values.get("__observed_source")
        if not isinstance(source, Mapping) or set(source) != {"path", "sha256", "byte_count"}:
            raise ValueError("source_path: Select the detector file and wait for its check to finish.")
        selected_path = normalized_tissue_source_path(raw_values.get("source_path"))
        if source["path"] != selected_path:
            raise ValueError("source_path: The selected detector file changed. Select it again before saving.")
        response["source"] = dict(source)
    return response


def submit_tissue_setup_form(form, action, raw_values):
    """Bind validated controls to the actual pending tissue request."""
    response = decode_tissue_setup_values(form, action, raw_values)
    result = StageResult.from_mapping(form.context["stage_result"])
    validate_interaction_response(result, action, response)
    return response


def build_saved_tissue_setup_form(saved):
    """Present accepted choices without a pending request or submission authority."""
    seed = saved.parameters.to_mapping()
    context = {"channels": saved.channels, "native_pyramid_inventory": saved.native_pyramid_inventory,
               "project_path": saved.project_path, "available_classifiers": (
                   {"classifier_name": seed["classifier_name"], "sha256": saved.classifier_sha256},)}
    selected = saved.resolution_selection
    form = tissue_setup_controls(
        context, seed, allowed_actions=("select_tissue_detector", "import_tissue_detector"),
        initial_native_resolution_index=None if selected is None else selected["native_resolution_index"],
    )
    initial_index = (str(selected["native_resolution_index"]) if selected is not None
                     else form.context["initial_native_resolution_index"])
    values = {**seed, "detector_source": "saved", "saved_classifier_name": seed["classifier_name"],
              "native_resolution_index": initial_index}
    return replace(form, message="Review the saved detector, image detail and smallest tissue region. Changes are applied only when you save.",
        controls=tuple(replace(control, value=values.get(control.name, control.value)) for control in form.controls),
        context={**form.context, "saved_tissue_settings": True,
                 "initial_native_resolution_index": initial_index})
