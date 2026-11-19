"""

Shows the fields required by the current page of tissue-detector setup.

We select the detector first, choose its image channel and resolution second,
and set the minimum retained tissue area third. The visibility rules follow
that sequence and the current detector/resolution method.
"""

DETECTOR_FIELDS = frozenset(
    {"detector_source", "saved_classifier_name", "source_path", "import_classifier_name"}
)
DETAIL_FIELDS = frozenset(
    {
        "reference_channel_id",
        "native_level_selection_mode",
        "native_resolution_index",
        "resolution_um_per_px",
        "native_level_match_tolerance_fraction",
        "allow_level0_selection",
    }
)
PAGE_TITLES = ("Choose detector", "Image detail", "Tissue regions")
PAGE_HELP = (
    "Create a detector in QuPath or select one already saved for this analysis.",
    "Select the channel and image resolution used to create the tissue detector.",
    "Choose which separate tissue regions are retained for analysis.",
)

# ==============================================================================
#                                  Tissue flow
# ==============================================================================


def show_field(name, step, values):
    """

    Checks whether a field belongs to the selected tissue-setup page and method.

    A saved detector shows its project selection; an imported detector shows a file
    and import name. The image-detail page shows one native-level selector.
    Selecting level zero explicitly enables full resolution. The area field
    appears on the final page.

    Args:
        name: Form field to consider.
        step: Page index: 0 for detector, 1 for image detail and 2 for tissue regions.
        values: Current editor values determining detector and resolution choices.

    Returns:
        True when this page and method require the field to be shown.
    """

    source = values.get("detector_source")
    if name == "tissue_image_features":
        return step == 1 and source in {"rf", "svm"}
    if name in DETECTOR_FIELDS:
        return step == 0 and (
            name == "detector_source"
            or name == "saved_classifier_name"
            and source == "saved"
            or name in {"source_path", "import_classifier_name"}
            and source == "import"
        )
    if name == "minimum_object_area_um2":
        return step == 2
    if name not in DETAIL_FIELDS or step != 1:
        return False
    return name in {"reference_channel_id", "native_resolution_index"}



def validate_detail(form, values):
    """

    Checks the reference channel and resolution before advancing to Tissue Regions.

    We require a channel and a native level from the current image. Submission
    preserves accepted physical matching when the displayed level is unchanged.

    Args:
        form: Current form containing the permitted channels and resolution choices.
        values: Values entered on the image-detail page.

    Raises:
        ValueError: A required image choice is missing or its numerical value is invalid.
    """
    controls = {c.name: c for c in form.controls}
    if values.get("reference_channel_id") not in controls["reference_channel_id"].choices:
        raise ValueError("reference_channel_id: Select the channel used by this detector.")
    index = values.get("native_resolution_index")
    if index not in controls["native_resolution_index"].choices:
        raise ValueError(
            "native_resolution_index: Choose one of the available image resolutions."
        )
