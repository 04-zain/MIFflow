"""

Collects the registration method and cluster fitting settings.

Desktop methods open the same registration editor for global alignment, the
local-refinement decision and final image review. Cluster execution retains its
complete automatic fitting settings because the calculations run remotely.

"""

from .interactions import FormModel, FormControl, ControlKind, decode_control_value
from mif_pipeline.registration.automatic import (
    AutomaticAlignmentSettings,
    BSplineAlignmentSettings,
)


def build_alignment_form(result, *, remote_execution=False):
    """

    Builds the registration choices for the selected mIF and paired-image entries.

    The desktop collects only the method; the editor owns the fitting controls.
    Remote execution also collects the settings needed by the cluster fitter.

    Args:
        result: Pending alignment request containing the paired project entries
            and actions available at this step.
        remote_execution: Whether the cluster must receive complete fitting settings.

    Returns:
        FormModel with the method choice and, for remote execution, fitting
        parameters initialized from the registration module's configuration.
    """

    defaults = AutomaticAlignmentSettings()
    context = result.resume_context or {}
    context = context.get("service_context", context)
    channels = tuple(context.get("fixed_channels", ()))
    from .feature_channels import channel_choice_labels
    channel_labels = channel_choice_labels(channels)
    fixed_choices = tuple(str(index + 1) for index in range(len(channels)))
    moving_channels = tuple(context.get("moving_channels", ()))
    moving_choices = tuple(str(index) for index in range(len(moving_channels) + 1))
    controls = [
        FormControl(
            "alignment_method", "Registration method", ControlKind.CHOICE,
            choices=("manual", "automatic"), value="manual",
            help_text="",
        ),
        FormControl(
            "alignment_fixed_channel", "mIF fitting signal", ControlKind.CHOICE if channels else ControlKind.INTEGER,
            choices=fixed_choices, value=str(defaults.fixed_channel + 1) if channels else defaults.fixed_channel + 1,
            help_text="Select the signal showing corresponding tissue structure.",
        ),
        FormControl(
            "alignment_moving_channel", "Paired-image fitting signal", ControlKind.CHOICE if moving_channels else ControlKind.INTEGER,
            choices=moving_choices if moving_channels else (),
            value=str(defaults.moving_channel + 1) if moving_channels else defaults.moving_channel + 1,
            help_text="Use combined image contrast or select the channel showing corresponding tissue structure.",
        ),
        FormControl(
            "alignment_max_dimension", "Global view size (pixels)", ControlKind.INTEGER,
            value=defaults.max_dimension,
            help_text="Maximum width or height read for global affine fitting. The three optimizer stages compare progressively less-smoothed versions of this view.",
        ),
        FormControl(
            "alignment_iterations", "Global updates per resolution", ControlKind.INTEGER,
            value=defaults.iterations,
            help_text="Maximum optimizer updates at each affine image resolution.",
        ),
        FormControl(
            "alignment_threads", "Fitting CPU threads", ControlKind.INTEGER,
            value=defaults.threads,
            help_text="CPU threads used by the native Elastix and Transformix calculations.",
        ),
        FormControl(
            "alignment_orientations", "Compare four starting orientations", ControlKind.CHECKBOX,
            value=defaults.orientation_search,
            help_text="Fit from 0°, 90°, 180° and 270° and retain the successful affine with the lowest minimized image metric.",
        ),
        FormControl(
            "alignment_local_refinement", "Refine local alignment with a B-spline", ControlKind.CHECKBOX,
            value=defaults.local_refinement,
            help_text="Fit spatially varying displacement after the affine. Clear this choice to retain the global alignment.",
        ),
        FormControl(
            "alignment_bspline_max_dimension", "Refinement view size (pixels)", ControlKind.INTEGER,
            value=defaults.bspline.max_dimension,
            help_text="Maximum width or height of the finer image view supplied to local fitting.",
        ),
        FormControl(
            "alignment_bspline_iterations", "Refinement updates per resolution", ControlKind.INTEGER,
            value=defaults.bspline.iterations,
            help_text="Maximum updates at each of the three smoothing and control-grid levels.",
        ),
        FormControl(
            "alignment_grid_spacing_um", "Final control-point spacing (µm)", ControlKind.NUMBER,
            value=defaults.bspline.grid_spacing_um,
            help_text="Physical distance between neighbouring displacement control points. Smaller spacing adds parameters and permits shorter spatial changes.",
        ),
        FormControl(
            "alignment_bending_weight", "Bending penalty weight", ControlKind.NUMBER,
            value=defaults.bspline.bending_weight,
            help_text="Relative gradient weight for the penalty on rapidly varying displacement. The allowed interval is 0 to 1; zero removes that contribution.",
        ),
        FormControl(
            "alignment_maximum_displacement_um", "Maximum local shift (µm)", ControlKind.NUMBER,
            value=defaults.bspline.maximum_displacement_um,
            help_text="Largest sampled physical displacement permitted beyond the global affine.",
        ),
    ]
    if not remote_execution:
        controls = controls[:1]
    return FormModel(
        "alignment_setup", "Align the paired image",
        "",
        result.allowed_actions, controls=tuple(controls),
        context={
            "adapter": "alignment_setup", "stage_result": result.to_mapping(),
            "remote_execution": remote_execution,
            "qupath_project_path": context.get("project_file"), "resume_context": context,
            "choice_labels": {"alignment_method": {
                "manual": "Manual registration", "automatic": "Automatic registration",
            }, "alignment_fixed_channel": {str(index + 1): channel_labels[channel["channel_id"]]
                                             for index, channel in enumerate(channels)},
                "alignment_moving_channel": {
                    "0": "RGB stain contrast" if context.get("moving_is_rgb") else "Combined fluorescence",
                    **{str(index + 1): name for index, name in enumerate(moving_channels)},
                }},
        },
    )


def alignment_options(form, raw):
    """

    Validates the method and supplies settings only for remote automatic fitting.

    Only enabled stages contribute entered numerical fields. With local
    refinement disabled, unfinished hidden B-spline fields do not prevent a
    global-only fit. The returned automatic configuration records a complete
    checked setting for each stage, including the explicit refinement choice.

    Args:
        form: Current alignment FormModel.
        raw: Entered field values keyed by their form-control names.

    Returns:
        None for either desktop editor method or remote manual editing; otherwise
        complete remote automatic settings. Software setup resolves the programs.

    Raises:
        ValueError: The selected route, channel or active numerical values are invalid.
    """

    selected = raw.get("alignment_method", "manual")
    if selected not in {"manual", "automatic"}:
        raise ValueError("Select manual or automatic registration.")
    if selected == "manual" or not form.context.get("remote_execution"):
        return None
    controls = {item.name: item for item in form.controls}

    def value(name):
        """

        Decodes an active control using its entered value or initial default.

        Args:
            name: Name of a control present in the current alignment form.

        Returns:
            Typed value checked by that control's integer, number or checkbox decoder.
        """

        control = controls[name]
        decoded = decode_control_value(control, raw.get(name, control.value))
        if control.kind is ControlKind.CHOICE and decoded not in control.choices:
            raise ValueError(f"Select one of the offered choices for {control.label}.")
        return decoded

    refine = value("alignment_local_refinement")
    local = BSplineAlignmentSettings()
    if refine:
        local = BSplineAlignmentSettings(
            max_dimension=value("alignment_bspline_max_dimension"),
            iterations=value("alignment_bspline_iterations"),
            grid_spacing_um=value("alignment_grid_spacing_um"),
            bending_weight=value("alignment_bending_weight"),
            maximum_displacement_um=value("alignment_maximum_displacement_um"),
        )
    settings = AutomaticAlignmentSettings(
        fixed_channel=int(value("alignment_fixed_channel")) - 1,
        moving_channel=int(value("alignment_moving_channel")) - 1,
        max_dimension=value("alignment_max_dimension"), iterations=value("alignment_iterations"),
        threads=value("alignment_threads"), orientation_search=value("alignment_orientations"),
        local_refinement=refine, bspline=local,
    )
    if settings.fixed_channel < 0:
        raise ValueError("Select the mIF channel using its number from 1.")
    # The executing computer resolves its installation from Software setup.
    return {**settings.to_mapping(), "tools_directory": None}


def submit_alignment_form(form, action, raw):
    """

    Checks the offered action and selected registration settings before submission.

    Registration fitting and the subsequent acceptance use separate operations.
    This submission validates the active route without adding image-entry or
    completed-review fields to the response expected by the pending request.

    Args:
        form: Current alignment form and its permitted actions.
        action: Offered action selected by the Save button.
        raw: Current registration fields.

    Returns:
        Empty response dictionary for the action; automatic fitting options
        are collected separately by the controller.

    Raises:
        ValueError: The action is unavailable or its active settings are invalid.
    """

    if action not in form.allowed_actions:
        raise ValueError("This alignment action is not offered.")
    if action == "import_warpy_transform":
        alignment_options(form, raw)
    return {}
