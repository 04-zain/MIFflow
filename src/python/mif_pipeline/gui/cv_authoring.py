"""

Builds the classifier's parameter-grid controls and translates their values into a search.

A grid lists the values to compare for each hyperparameter. Every applicable
combination becomes a complete model configuration, which is fitted and evaluated
within each subject fold. After comparing candidates, the selected configuration
is fitted again using all development subjects.

The form shows the number of complete fits before submission. Fixed-setting
procedures use one fit; cross-validation uses candidates × folds + one final refit.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from .interactions import FormControl, ControlKind
from mif_pipeline.orchestration.batch_training.cross_validation import QuPathGroupedCVConfiguration
from mif_pipeline.pipeline_core.model_selection import subject_folds

# ==============================================================================
#                                  Cv authoring
# ==============================================================================


def apply_current_tissue_defaults(controls):
    """

    Populates new classifier controls with the shared feature-processing presets.

    The controls are identified by their field suffixes. This assigns the same starting
    values for observation limits, feature scaling, PCA and annotation boundaries
    wherever those classifier settings are displayed. Boolean presets are converted to
    the "yes"/"no" values used by the form choices.

    Args:
        controls: Classifier controls to populate with starting values and field guidance.

    Returns:
        The controls with updated presets, labels and help text.
    """

    from dataclasses import replace
    from mif_pipeline.orchestration.batch_training.qupath_feature_settings import (
        pixel_feature_authoring_defaults,
    )

    values = pixel_feature_authoring_defaults()
    specification = {
        "maximum_samples": (
            "maximum_training_samples",
            "Maximum training observations",
            "Use 0 to fit all eligible labelled pixels. A positive limit fits a random sample of up to that many pixels.",
        ),
        "reweight": (
            "reweight_samples",
            "Balance class contributions",
            "Give each class the same total weight by assigning more weight to pixels from less frequent classes.",
        ),
        "normalization": (
            "normalization",
            "Feature scaling",
            "Standardization subtracts each feature’s training mean and divides by its standard deviation; min–max scales its training range to 0–1. Prediction reuses the fitted scaling; cross-validation fits it within each training fold.",
        ),
        "pca_variance": (
            "pca_retained_variance",
            "PCA variance target",
            "The target sets how much variation in the training data the retained components should capture.",
        ),
        "pca_normalize": (
            "pca_normalize",
            "PCA whitening",
            "Divide each component score by its training standard deviation so that the retained components have the same variance.",
        ),
        "boundary_strategy": (
            "boundary_strategy",
            "Annotation boundary treatment",
            "Keep the annotated region labels at their edges, or assign an edge band to a separate boundary class.",
        ),
    }
    result = []
    for control in controls:
        for suffix, (key, label, help_text) in specification.items():
            if control.name.endswith("_" + suffix):
                value = values[key]
                if type(value) is bool:
                    value = "yes" if value else "no"
                control = replace(control, value=value, label=label, help_text=help_text)
                break
        if control.name.startswith("training_") and control.name.endswith("_resolution"):
            control = replace(
                control,
                label="Training pixel size (µm)",
                help_text="Pixel size used when extracting image features. Smaller values retain finer detail and increase processing time and memory.",
            )
        result.append(control)
    return result


def search_controls(index: int, subjects: int) -> tuple[FormControl, ...]:
    """

    Creates the fold-count, GNN-dimension and hyperparameter-grid fields for a classifier.

    A fold count of zero asks the training code to choose up to five folds from the
    available development subjects. GNN feature dimensions add separate spatial-model
    fits to a KRONOS search. The renderer shows the fields applicable to the chosen method.

    Args:
        index: Classifier position used to prefix its field names.
        subjects: Subject count supplied by the form builder. Fold feasibility is
            checked from the assigned development subjects at submission.

    Returns:
        A tuple of controls for this classifier's search settings.
    """

    from mif_pipeline._defaults import default_section

    defaults = default_section("classifier.training")
    prefix = f"training_{index}"
    return (
        FormControl(
            prefix + "_cv_folds",
            "Validation folds",
            ControlKind.INTEGER,
            required=False,
            value=defaults["subject_folds"],
            help_text="Each fold holds out whole subjects. Every training fold must include all classes.",
        ),
        FormControl(
            prefix + "_cv_graph_widths",
            "GNN feature dimensions to compare",
            ControlKind.TEXT,
            required=False,
            help_text="Feature counts to compare, such as 32, 64. Leave blank to use the selected dimension. Each value fits a graph model within each validation fold.",
        ),
    ) + grid_controls(index)


def configure_search(base: dict, values: dict, index: int, development_subjects: list[str]) -> dict:
    """

    Builds the fitting configuration for the selected training procedure.

    For cross-validation, first check that the assigned subjects can form the requested
    folds. Expand the entered grid into complete candidate settings and record the
    scoring rule with those candidates. The training service uses this record for
    comparison and final refitting. A fixed-setting procedure uses the entered model.

    Args:
        base: Complete model settings from which grid candidates are constructed.
        values: Current classifier form values.
        index: Classifier position used to locate its procedure, grid and fold fields.
        development_subjects: Subjects available for training and cross-validation.

    Returns:
        Fixed model settings or a grouped-cross-validation configuration containing
        the candidate list, requested folds and scoring rule.

    Raises:
        ValueError: The procedure, folds or candidate settings are invalid.
    """

    from .training_procedure import selected_procedure

    # Use the selected procedure to decide whether to build a search.
    if not selected_procedure(values, index).cross_validation:
        return base
    from mif_pipeline.orchestration.batch_training.search_grid import expand_grid

    prefix = f"training_{index}"
    folds = values.get(prefix + "_cv_folds", 0)
    subject_folds(development_subjects, folds, values.get(prefix + "_seed", 0))
    candidates = expand_grid(
        base,
        grid_fields(values, index),
        graph_widths=values.get(prefix + "_cv_graph_widths"),
    )
    return QuPathGroupedCVConfiguration.from_mapping(
        {
            "configuration_schema": "qupath-grouped-cross-validation/v1",
            "folds": folds,
            "scoring": "subject_weighted_macro_f1/v1",
            "candidates": candidates,
        }
    ).to_mapping()


# ==============================================================================
#                Editable parameter lists and the fitting summary
# ==============================================================================


def grid_controls(index):
    """

    Creates editable comma-separated parameter lists from the packaged grid presets.

    RF exposes tree depth, minimum samples, features per split and tree count. SVM
    exposes kernel, C and RBF gamma. Logistic regression exposes regularization,
    learning rate and iteration count. A single entered value keeps that parameter
    fixed while other parameters are compared.

    Args:
        index: Classifier position used to give its controls unique names.

    Returns:
        The model grid text fields with their starting values and explanations.
    """

    from mif_pipeline.orchestration.batch_training.search_grid import DEFAULT_GRID

    from .control_guidance import RF_PARAMETER_HELP

    labels = {
        "rtrees_max_depth": (
            "Tree depths",
            RF_PARAMETER_HELP["max_depth"],
        ),
        "rtrees_min_sample_count": (
            "Minimum samples per node",
            RF_PARAMETER_HELP["min_sample_count"],
        ),
        "rtrees_active_var_count": (
            "Features considered per split",
            "Number of randomly selected features considered at each split. The default uses the square root of the number of input features, rounded to a whole number.",
        ),
        "rtrees_max_trees": ("Number of trees", RF_PARAMETER_HELP["max_trees"]),
        "svm_kernels": (
            "SVM kernels",
            "Linear gives a linear decision boundary. RBF allows a curved boundary.",
        ),
        "svm_c": (
            "SVM C values",
            "Larger C penalizes training observations inside the margin or on the wrong side of the boundary more strongly.",
        ),
        "svm_gamma": (
            "RBF gamma values",
            "Higher gamma limits each observation’s influence to a smaller neighbourhood in feature space.",
        ),
        "logistic_regression_regularizations": (
            "Logistic regularization choices",
            "L2 penalizes large coefficients. Disable removes that penalty.",
        ),
        "logistic_regression_learning_rate": (
            "Logistic learning rates",
            "Step size for batch gradient descent. The effective update also depends on the number of training cells.",
        ),
        "logistic_regression_iterations": (
            "Logistic iteration counts",
            "Number of batch updates for each one-versus-rest model.",
        ),
    }
    return tuple(
        FormControl(
            f"training_{index}_grid_{key}",
            label,
            ControlKind.TEXT,
            required=False,
            value=DEFAULT_GRID[key],
            help_text=detail,
        )
        for key, (label, detail) in labels.items()
    )


def grid_fields(values, index):
    """

    Collects the grid text for one classifier, using presets for fields absent from the form.

    Args:
        values: Form values keyed by control name.
        index: Classifier position whose grid should be read.

    Returns:
        Parameter names mapped to their entered comma-separated text or preset text.
    """

    from mif_pipeline.orchestration.batch_training.search_grid import DEFAULT_GRID

    prefix = f"training_{index}_grid_"
    return {key: values.get(prefix + key, default) for key, default in DEFAULT_GRID.items()}


def seed_grid_baseline(values, index):
    """

    Fills the base model's searched parameters from the first entered grid combination.

    The complete model parser needs one scalar value for every required parameter.
    Using the first combination provides those values before expand_grid() constructs
    all candidates. Parameters outside the grid retain their separately entered settings.

    Args:
        values: Mutable form-value dictionary updated with the first combination.
        index: Classifier position identifying its method and grid fields.

    Raises:
        ValueError: The entered grid cannot form a valid bounded set of combinations.
    """

    from mif_pipeline.orchestration.batch_training.search_grid import parameter_combinations

    prefix = f"training_{index}"
    model_prefix = f"supervised_{index}" if f"supervised_{index}_model_method" in values else prefix
    method = values.get(model_prefix + "_model_method", "rtrees")
    if model_prefix.startswith("supervised_"):
        method = method.removeprefix("kronos_")
    rows = parameter_combinations(
        method,
        grid_fields(values, index),
        graph_widths=values.get(prefix + "_cv_graph_widths"),
    )

    # Use the first grid combination to supply the base model’s searched parameters.
    for family, settings in rows[0].items():
        if family == "graph":
            values[prefix + "_kronos_width"] = settings["hidden_width"]
        else:
            for name, value in settings.items():
                values[f"{model_prefix}_{family}_{name}"] = value


class TrainingSearchError(ValueError):
    """Associate a fitting-summary failure with the control that can correct it."""

    def __init__(self, message: str, control_name: str):
        super().__init__(message)
        self.control_name = control_name


def grid_summary(values, index, development_subject_count, *, include_parameters=False):
    """

    Calculates how many complete fits the selected training procedure requires.

    For cross-validation, each candidate is fitted once per subject fold, followed by
    one fit of the selected configuration on all development subjects. Four candidates
    and five folds therefore require 4 × 5 + 1 = 21 fits. GNN dimension choices are
    included in the candidate count.

    Args:
        values: Current procedure, method, fold and grid entries.
        index: Classifier position used to locate these fields.
        development_subject_count: Number of subjects remaining after test reservation
            and exclusions.

    Returns:
        A dictionary with candidate, fold and total-fit counts.

    Raises:
        ValueError: The procedure, grid or fold count is invalid or there are too few
            development subjects for cross-validation.
    """

    from mif_pipeline.orchestration.batch_training.search_grid import parameter_combinations

    prefix = f"training_{index}"
    from .training_procedure import selected_procedure

    if not selected_procedure(values, index).cross_validation:
        return {"candidates": 1, "folds": 0, "fits": 1}
    raw = values.get(prefix + "_cv_folds", 0)
    try:
        folds = int(raw)
    except (ValueError, TypeError) as error:
        raise TrainingSearchError("Enter a whole-number subject-fold count.", prefix + "_cv_folds") from error
    if folds == 0:
        folds = min(5, development_subject_count)
    if not 2 <= folds <= min(10, development_subject_count):
        raise TrainingSearchError(
            "Cross-validation needs at least two development subjects and a feasible fold count.",
            prefix + ("_assignment_mode" if development_subject_count < 2 else "_cv_folds"),
        )
    method = (values[f"supervised_{index}_model_method"].removeprefix("kronos_")
              if f"supervised_{index}_model_method" in values
              else values.get(prefix + "_model_method", "rtrees"))
    try:
        rows = parameter_combinations(
            method,
            grid_fields(values, index),
            graph_widths=values.get(prefix + "_cv_graph_widths"),
        )
    except (ValueError, TypeError) as error:
        key = str(error).split(":", 1)[0].lower().replace(" ", "_")
        key = "logistic_regression_regularizations" if key == "logistic_regularization" else key
        field = prefix + ("_cv_graph_widths" if key == "spatial_feature_widths" else "_grid_" + key)
        if field not in values:
            family = str(method).removeprefix("kronos_")
            field = next((name for name in values if name.startswith(prefix + "_grid_" + family + "_")), prefix + "_procedure")
        raise TrainingSearchError(str(error), field) from error
    count = len(rows)
    summary = {"candidates": count, "folds": folds, "fits": count * folds + 1}
    if include_parameters:
        summary["parameters"] = rows
    return summary
