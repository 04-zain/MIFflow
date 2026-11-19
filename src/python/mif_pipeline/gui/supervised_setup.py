"""Presentation controls for an explicit supervised training selection.

The interface keeps editable researcher choices associated with the current image or
analysis request. Values are passed to the stage-specific validator before they become
accepted processing settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math

from mif_pipeline.gui.interactions import ControlKind, FormControl
from mif_pipeline.orchestration.batch_training.qupath_rtrees import (
    QuPathObjectRTreesConfiguration,
    QuPathRTreesParameters,
)

# ==============================================================================
#                                Supervised setup
# ==============================================================================


def supervised_controls(
    index: int, options: Mapping[str, object], *, intensity_divisor=None
) -> tuple[list[FormControl], dict[str, dict[str, str]]]:
    """Offer cell inputs and native classifier settings with shared method guidance.

    The current measurement/channel inventory supplies selectable identities.
    KRONOS inputs are offered only when the processed channel inventory exists;
    scientific extraction choices are validated when that source is selected.
    """
    from mif_pipeline._defaults import default_section

    feature_defaults = default_section("phenotyping.supervised")
    prefix = f"supervised_{index}"
    configured = options["configured_class_ids"]
    features = options["available_features"]
    if not isinstance(features, Sequence):
        raise ValueError("supervised feature inventory is missing")
    ids = tuple(row["feature_id"] for row in features)
    labels = {
        row["feature_id"]: (
            f"{row['measurement_name']} ({row['compartment']})"
            if row["compartment"]
            else row["measurement_name"]
        )
        for row in features
    }
    controls = [
        FormControl(
            f"{prefix}_model_method", "Classifier", ControlKind.CHOICE,
            choices=("rtrees", "svm", "logistic_regression") + (("kronos_rtrees", "kronos_svm", "kronos_logistic_regression") if options.get("available_channels") else ()), value="rtrees",
            help_text="Choose how the classifier learns phenotype labels from the selected inputs.",
        ),
        FormControl(
            f"{prefix}_class_ids",
            "Phenotype names",
            ControlKind.OBJECT_SEQUENCE,
            value=() if configured is None else configured,
            help_text=(
                "Enter the exact QuPath point annotation labels you will draw."
                if configured is None
                else "Imported labels are fixed for this training plan."
            ),
        ),
        FormControl(
            f"{prefix}_feature_ids",
            "Cell measurements for training",
            ControlKind.OBJECT_SEQUENCE,
            choices=ids,
            value=(),
            required=False,
            help_text="Select the cell measurements used to fit the phenotype classifier.",
        ),
        FormControl(
            f"{prefix}_point_radius",
            "Point annotation radius (full-resolution pixels)",
            ControlKind.NUMBER,
            value=feature_defaults["point_selection_radius_level0"],
            help_text="Maximum distance from a newly labelled point to the nearest eligible cell centre. Once saved, the label stays linked to that cell's ID.",
        ),
        FormControl(
            f"{prefix}_normalization",
            "Feature scaling",
            ControlKind.CHOICE,
            choices=("none", "mean_variance", "min_max"),
            value=feature_defaults["normalization"],
            help_text="Scaling applied to classifier inputs before training. Mean–variance centers and standardizes each feature; min–max scales its training range to 0–1.",
        ),
        FormControl(
            f"{prefix}_pca_variance",
            "PCA variance target",
            ControlKind.NUMBER,
            value=feature_defaults["pca_retained_variance"],
            help_text="The target sets how much variation in the training data the retained components should capture.",
        ),
        FormControl(f"{prefix}_pca_normalize", "PCA whitening", ControlKind.CHOICE,
                    choices=("yes", "no"), value="yes"),
    ]
    defaults = QuPathRTreesParameters.authoring_defaults().to_mapping()
    from .control_guidance import RF_PARAMETER_HELP

    help_text = RF_PARAMETER_HELP
    for field, label, kind in (
        ("max_depth", "Maximum tree depth", ControlKind.INTEGER),
        ("min_sample_count", "Minimum samples per node", ControlKind.INTEGER),
        ("active_var_count", "Features considered per split", ControlKind.INTEGER),
        ("max_trees", "Maximum trees", ControlKind.INTEGER),
        ("termination_epsilon", "Out-of-bag error threshold", ControlKind.NUMBER),
        ("calculate_variable_importance", "Calculate variable importance", ControlKind.CHECKBOX),
    ):
        controls.append(
            FormControl(
                f"{prefix}_rtrees_{field}",
                label,
                kind,
                value=defaults[field],
                help_text=help_text[field],
            )
        )
    from mif_pipeline.orchestration.batch_training.qupath_svm import QuPathSvmParameters
    svm = QuPathSvmParameters.authoring_defaults().to_mapping()
    for field, label, kind, help_text in (
        ("kernel", "SVM kernel", ControlKind.CHOICE, "Linear gives linear boundaries in feature space. RBF permits nonlinear boundaries, controlled by C and gamma."),
        ("c", "SVM C", ControlKind.NUMBER, "Higher C penalizes training observations inside the margin or on the wrong side of the boundary more strongly."),
        ("gamma", "RBF gamma", ControlKind.NUMBER, "Higher gamma restricts similarity to observations closer together in feature space."),
        ("max_iterations", "Maximum SVM iterations", ControlKind.INTEGER, "Maximum number of fitting iterations for each pair of classes."),
        ("termination_epsilon", "SVM termination tolerance", ControlKind.NUMBER, "Tolerance for solving the SVM optimization problem. Smaller values request a closer solution and may require more fitting iterations."),
    ):
        controls.append(FormControl(f"{prefix}_svm_{field}", label, kind,
            choices=("LINEAR", "RBF") if field == "kernel" else (),
            value=svm[field], required=False, help_text=help_text))
    from mif_pipeline.orchestration.batch_training.qupath_logistic_regression import QuPathLogisticRegressionParameters
    logistic = QuPathLogisticRegressionParameters.authoring_defaults().to_mapping()
    for field, label, kind, help_text in (
        ("regularization", "Logistic regression regularization", ControlKind.CHOICE, "L2 penalizes large coefficients. Disable removes that penalty."),
        ("learning_rate", "Logistic regression learning rate", ControlKind.NUMBER, "Step size for native batch gradient descent. The effective update also depends on the number of training cells."),
        ("iterations", "Logistic regression iterations", ControlKind.INTEGER, "Number of batch updates for each one-versus-rest model. OpenCV performs this many updates; it does not stop on a convergence tolerance."),
    ):
        controls.append(FormControl(f"{prefix}_logistic_regression_{field}", label, kind,
            choices=("L2", "DISABLE") if field == "regularization" else (),
            value=logistic[field], required=False, help_text=help_text))
    from .kronos_cell_setup import cell_kronos_controls
    kronos_controls, kronos_labels = (cell_kronos_controls(index, options["available_channels"],
                                        pixel_size_um=options.get("recommended_pixel_size_um"), nuclear_channel_id=options.get("nuclear_channel_id"), intensity_divisor=intensity_divisor)
                                      if options.get("available_channels") else ([], {}))
    controls.extend(kronos_controls)
    return controls, {
        f"{prefix}_feature_ids": labels,
        f"{prefix}_model_method": {"rtrees": "Random forest (RF)", "svm": "Support vector machine (SVM)", "logistic_regression": "Logistic regression (LR)", "kronos_rtrees": "KRONOS + random forest", "kronos_svm": "KRONOS + SVM", "kronos_logistic_regression": "KRONOS + logistic regression"},
        **kronos_labels,
    }


def prefill_supervised_controls(controls, context, request, *, subjects):
    """Restore the previous accepted cell setup into the currently offered form.

    Measurements are matched by their complete names; subject roles additionally
    require the same entry, sample and subject identities. Only the selected
    estimator's settings are restored. Other method controls keep their normal
    defaults, so switching method cannot carry incompatible parameters with it.
    The caller supplies the request from the explicit settled-review predecessor.
    """
    from dataclasses import replace

    options = context["supervised_options"]
    configuration = request["model_configuration"]
    cross_validation = configuration.get("configuration_schema") == "qupath-grouped-cross-validation/v1"
    candidates = configuration["candidates"] if cross_validation else (configuration,)
    model = candidates[0]
    method = next(name for name in ("rtrees", "svm", "logistic_regression") if name in model)
    source = model.get("feature_source")
    prefix = "supervised_0_"
    values = {
        prefix + "model_method": ("kronos_" if source is not None else "") + method,
        prefix + "point_radius": request["feature_requirements"]["point_selection_radius_level0"],
        prefix + "normalization": model["normalization"],
        prefix + "pca_variance": model["pca_retained_variance"],
        prefix + "pca_normalize": "yes" if model.get("pca_normalize", True) else "no",
        "training_0_seed": request["random_seed"],
        "training_0_procedure": ("training_validation" if cross_validation else "training") +
            ("_test" if request["evaluation_intent"] == "independent_performance" else ""),
    }
    if values["training_0_procedure"] == "training":
        values["training_0_procedure"] = "training_only"
    if options["configured_class_ids"] is None:
        values[prefix + "class_ids"] = list(request["required_classes"])
    if source is None:
        offered = {row["measurement_name"]: row["feature_id"] for row in options["available_features"]}
        values[prefix + "feature_ids"] = [offered[row["measurement_name"]]
            for row in request["feature_requirements"]["feature_schema"]
            if row["measurement_name"] in offered]
    values.update({prefix + method + "_" + key: value for key, value in model[method].items()})

    participants = context["candidate_participants"]
    retained = {row["project_entry_id"]: row for row in request["entries"]}
    test_subjects = 0
    for index, entries in enumerate(subjects.values()):
        roles = set()
        for entry in entries:
            old = retained.get(entry)
            current = participants[entry]
            roles.add(old["split"] if old is not None and all(
                old[key] == current[key] for key in ("sample_id", "subject_id")) else "not_selected")
        role = next(iter(roles)) if len(roles) == 1 else "not_selected"
        values[f"training_0_patient_{index}_role"] = role
        test_subjects += role == "test"
    values["training_0_test_subjects"] = test_subjects

    if cross_validation:
        values["training_0_cv_folds"] = configuration["folds"]
        for control in controls:
            field = control.name.removeprefix("training_0_grid_" + method + "_")
            if field == control.name:
                continue
            field = {"kernels": "kernel", "regularizations": "regularization"}.get(field, field)
            selected = list(dict.fromkeys(candidate[method][field] for candidate in candidates
                                         if candidate[method].get(field) is not None))
            if selected:
                values[control.name] = ", ".join(str(value) for value in selected)

    if source is not None:
        encoder = source["encoder"]
        rows = options.get("available_channels", ())
        offered = {row["channel_id"]: (index, row) for index, row in enumerate(rows)}
        selected = [channel for channel, name in zip(encoder["channel_ids"], source["channel_names"])
                    if channel in offered and offered[channel][1]["name"] == name]
        key = prefix + "kronos_"
        values.update({key + "channel_ids": selected, key + "patch_size": str(encoder["patch_size"]),
            key + "pixel_x_um": encoder["pixel_size_um"][0], key + "pixel_y_um": encoder["pixel_size_um"][1],
            key + "threads": encoder["threads"], key + "memory_mib": encoder["activation_budget_bytes"] // 1024**2})
        from mif_pipeline.compartment_analysis.kronos_marker_preparation import marker_key
        additional = {marker_key(row["marker_name"]): row for row in encoder["additional_markers"]}
        for position, channel in enumerate(encoder["channel_ids"]):
            if channel not in selected:
                continue
            panel = key + f"panel_{offered[channel][0]}_"
            marker = encoder["marker_names"][position]
            values[panel + "marker"] = marker
            values[panel + "divisor"] = encoder["intensity_divisors"][position]
            values.update({panel + "additional_" + name: value
                           for name, value in additional.get(marker_key(marker), {}).items() if name != "marker_name"})

    return [replace(control, value=values[control.name])
            if control.name in values and (control.kind is not ControlKind.CHOICE
                or values[control.name] in control.choices) else control for control in controls]


def supervised_selection(
    decoded: Mapping[str, object],
    index: int,
    options: Mapping[str, object],
    *, kronos_setup=None, kronos_extraction=None, kronos_saved_encoder=None,
) -> tuple[list[str], list[str], dict[str, object]]:
    """Build a checked cell model from the selected source, classes and head.

    Measured features must come from the offered inventory. KRONOS supplies its
    fixed ordered 768-feature schema and an inspected extraction contract. The
    separate point radius follows the same full-resolution reference rule for
    every head; cross-validation wraps this checked configuration downstream.
    """
    prefix = f"supervised_{index}"
    classes = decoded.get(f"{prefix}_class_ids")
    features = decoded.get(f"{prefix}_feature_ids")
    configured = options["configured_class_ids"]
    if (
        not isinstance(classes, (list, tuple))
        or len(classes) < 2
        or any(
            type(item) is not str or not item.strip() or item != item.strip() for item in classes
        )
        or len(set(classes)) != len(classes)
    ):
        raise ValueError("Select at least two distinct exact QuPath phenotype annotation labels")
    if configured is not None and tuple(classes) != tuple(configured):
        raise ValueError("Imported phenotype labels cannot be changed")
    selected_method = decoded.get(f"{prefix}_model_method", "rtrees")
    backend = "kronos" if selected_method.startswith("kronos_") else "measurements"
    method = selected_method.removeprefix("kronos_")
    if method not in {"rtrees", "svm", "logistic_regression"}:
        raise ValueError("Choose random forest, SVM or logistic regression")
    source = None
    if backend == "kronos":
        from .kronos_cell_setup import build_cell_kronos_source
        from mif_pipeline.orchestration.batch_training.qupath_kronos_cell import kronos_cell_measurements
        source = build_cell_kronos_source(decoded, index, options.get("available_channels", ()),
            setup=kronos_setup, nuclear_channel_id=options.get("nuclear_channel_id"),
            extraction=kronos_extraction, saved_encoder=kronos_saved_encoder)
        features = [row["measurement_name"] for row in kronos_cell_measurements()]
    offered = (set(features) if backend == "kronos" else {row["feature_id"] for row in options["available_features"]})
    if (
        not isinstance(features, (list, tuple))
        or not features
        or any(type(item) is not str or item not in offered for item in features)
        or len(set(features)) != len(features)
    ):
        raise ValueError("Select distinct cell measurements from the available choices")
    radius = decoded.get(f"{prefix}_point_radius")
    if type(radius) not in (int, float) or not math.isfinite(radius) or radius <= 0:
        raise ValueError("Point annotation radius must be positive full-resolution pixels")
    defaults = QuPathRTreesParameters.authoring_defaults().to_mapping()
    rtrees = {
        field: decoded.get(f"{prefix}_rtrees_{field}", value)
        for field, value in defaults.items()
        if field not in {"cv_folds", "use_surrogates"}
    }
    rtrees.update({"cv_folds": 0, "use_surrogates": False})
    mapping = {
        "configuration_schema": "qupath-object-rtrees/v1",
        "object_filter": "cells",
        "training_annotations": "points",
        "output_classes": "selected",
        "normalization": decoded.get(f"{prefix}_normalization"),
        "pca_retained_variance": decoded.get(f"{prefix}_pca_variance"),
        "multiclass": False,
        "output_labels": {
            "schema": "qupath-output-labels/v1",
            "ordered_labels": [
                {"canonical_class_id": class_id, "qupath_label": number}
                for number, class_id in enumerate(sorted(classes))
            ],
        },
        "rtrees": rtrees,
    }
    if decoded.get(f"{prefix}_pca_normalize") == "no" and mapping["pca_retained_variance"]:
        mapping["pca_normalize"] = False
    if source is not None:
        mapping["feature_source"] = source
    if method == "rtrees":
        checked = QuPathObjectRTreesConfiguration.from_mapping(mapping).to_mapping()
    elif method == "svm":
        from mif_pipeline.orchestration.batch_training.qupath_svm import QuPathObjectSvmConfiguration, QuPathSvmParameters
        parameters = QuPathSvmParameters.authoring_defaults().to_mapping()
        for field in parameters:
            if field != "model_type":
                parameters[field] = decoded.get(f"{prefix}_svm_{field}", parameters[field])
        if parameters["kernel"] == "LINEAR":
            parameters["gamma"] = None
        mapping.pop("rtrees")
        mapping.update(configuration_schema="qupath-object-svm/v1", svm=parameters)
        checked = QuPathObjectSvmConfiguration.from_mapping(mapping).to_mapping()
    else:
        from mif_pipeline.orchestration.batch_training.qupath_logistic_regression import QuPathObjectLogisticRegressionConfiguration, QuPathLogisticRegressionParameters
        parameters = QuPathLogisticRegressionParameters.authoring_defaults().to_mapping()
        for field in ("learning_rate", "iterations", "regularization"):
            parameters[field] = decoded.get(f"{prefix}_logistic_regression_{field}", parameters[field])
        mapping.pop("rtrees")
        mapping.update(configuration_schema="qupath-object-logistic-regression/v1", logistic_regression=parameters)
        checked = QuPathObjectLogisticRegressionConfiguration.from_mapping(mapping).to_mapping()
    checked["point_selection_radius_level0"] = float(radius)
    return list(classes), list(features), checked
