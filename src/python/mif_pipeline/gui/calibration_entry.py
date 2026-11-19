"""Missing-only acquisition controls, from the IO owner's field inventory."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from .interactions import FormControl, ControlKind

HELP = {
    "exposure_seconds": "Exposure duration recorded for this channel. Enter seconds (for example, 2.71 ms is 0.00271 s).",
    "gain": "Detector gain multiplier used when this channel was acquired.",
    "binning": "Detector binning along one image dimension: enter 1 for 1 × 1 or 2 for 2 × 2.",
    "offsets": "Additive detector offset expressed in stored pixel-intensity units, before exposure and gain correction.",
}

# ==============================================================================
#                               Calibration entry
# ==============================================================================


CORRECTION_CHOICES = {
    "full": "Enter missing imaging settings",
    "exposure_only": "Correct for exposure time only",
}
EXPOSURE_ONLY_CONDITION = (
    "Both reference images and batch slides must be acquired with identical settings for gain, binning and illumination, "
    "and have the same detector configuration, including detector offset."
)


def calibration_controls(inventory, *, fixed_correction_mode=None):
    """Offer the correction choice before missing acquisition entries.

    Manual correction requires all missing settings. The desktop reveals only
    missing exposures when exposure-only is selected; its explanation records the
    condition under which reference images and batch slides can share that policy.
    A fixed reference policy omits the choice and unapplied rows, retaining the
    original inventory indices so the entered values bind to the same cells.
    """
    if not inventory["missing"] and fixed_correction_mode is None:
        return []
    if fixed_correction_mode not in {None, "full", "exposure_only", "as_stored"}:
        raise ValueError("Unsupported fixed intensity correction")
    controls = [] if fixed_correction_mode is not None else [FormControl(
        "calibration_correction_mode",
        "Intensity correction",
        ControlKind.CHOICE, choices=tuple(CORRECTION_CHOICES),
        help_text=("Some imaging settings are missing from the image metadata. "
                   "For exposure-only correction, " + EXPOSURE_ONLY_CONDITION[0].lower() + EXPOSURE_ONLY_CONDITION[1:]),
    )]
    for i, row in enumerate(inventory["missing"]):
        if fixed_correction_mode == "as_stored" or (
            fixed_correction_mode == "exposure_only" and row["field"] != "exposure_seconds"
        ):
            continue
        kind = {
            "number": ControlKind.NUMBER,
            "integer": ControlKind.INTEGER,
            "text": ControlKind.TEXT,
        }[row["kind"]]
        controls.append(
            FormControl(
                f"calibration_{i}",
                f"{row['channel_name']} - {row['label']} ({row['units']})",
                kind,
                help_text=HELP[row["field"]],
            )
        )
    if controls or fixed_correction_mode is not None:
        controls.append(
            FormControl(
                "calibration_source",
                "Source of imaging settings",
                ControlKind.TEXT,
                help_text="Identify the imaging worksheet, scanner settings or documented calibration used for these values.",
            )
        )
    return controls
