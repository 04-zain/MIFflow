"""

Expands RF, SVM and logistic parameter lists into validation candidates.

For RF, each tree depth is paired with every minimum sample count, feature count
and tree count. For SVM, each kernel is paired with every C value; RBF candidates
also vary gamma. Logistic cell candidates vary regularization, learning rate and
batch iteration count. Comparing multiple GNN dimensions repeats the applicable
tissue model combinations for each spatial representation.

The current limit is twelve complete candidates. Counting combinations before
expansion lets the form reject an excessively large search with its actual size.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from itertools import product
from collections.abc import Mapping
import copy
import math
import re

MAX_CANDIDATES = 12
from mif_pipeline._defaults import default_section

DEFAULT_GRID = default_section("classifier.grid")


# ==============================================================================
#                                  Search grid
# ==============================================================================


def grid_values(value: str, *, name: str, integer=False, minimum=0, positive=False) -> list:
    """

    Parses a comma-separated list of numerical values for one search parameter.

    Trim each item, require the expected integer or floating-point representation,
    then check its range and uniqueness. Repeated numerical values such as 1 and 1.0
    are rejected in a floating-point grid because they would repeat the same candidate.

    Args:
        value: Comma-separated parameter values.
        name: Parameter name shown in errors.
        integer: Whether every item must be a whole number.
        minimum: Lowest permitted value.
        positive: Whether each value must be strictly greater than minimum.

    Returns:
        Parsed numerical values in their entered order.

    Raises:
        ValueError: There are empty or repeated entries, more than twelve values,
            invalid numbers, or values outside the permitted range.
    """

    if not isinstance(value, str) or len(value) > 512:
        raise ValueError(f"{name}: enter a comma-separated list of numeric values.")
    tokens = [part.strip() for part in value.split(",")]
    if not 1 <= len(tokens) <= MAX_CANDIDATES or any(not item for item in tokens):
        raise ValueError(f"{name}: enter 1–12 values separated by commas, without empty entries.")
    result = []
    for token in tokens:
        if integer and not re.fullmatch(r"\d+", token):
            raise ValueError(f"{name}: use whole numbers.")
        try:
            item = int(token) if integer else float(token)
        except ValueError as error:
            raise ValueError(f"{name}: enter numeric values.") from error
        if (
            (integer and item > 2147483647)
            or not math.isfinite(item)
            or item < minimum
            or (positive and item <= minimum)
        ):
            raise ValueError(f"{name}: a value is outside its permitted range.")
        if item in result:
            raise ValueError(f"{name}: remove repeated values.")
        result.append(item)
    return result


def parameter_combinations(
    method: str, values: Mapping[str, object], *, graph_widths=None
) -> list[dict]:
    """

    Builds the distinct parameter combinations for the selected classifier family.

    RF combinations contain depth, minimum samples, features per split and tree count.
    SVM combinations contain kernel and C; only RBF combinations vary gamma. For example,
    two C values with LINEAR and RBF and three gamma values produce 2 + 2 × 3 = 8
    candidates. This avoids counting the linear kernel once for every irrelevant gamma.

    A KRONOS grid can additionally compare GNN feature dimensions. Each dimension
    is paired with every RF/SVM combination because changing the learned feature vector
    requires fitting the classifier again.

    Args:
        method: "rtrees", "svm", "logistic_regression", "kronos_rtrees" or "kronos_svm".
        values: Entered parameter lists, with missing fields supplied from presets.
        graph_widths: Optional comma-separated GNN feature dimensions for KRONOS.

    Returns:
        A list of parameter dictionaries to insert into complete model configurations.

    Raises:
        ValueError: A field is invalid, the method is unsupported, or the combined
            search contains more than twelve candidates.
    """

    if method not in {"rtrees", "svm", "logistic_regression", "kronos_rtrees", "kronos_svm"}:
        raise ValueError("Choose a supported RF, SVM or logistic regression method before defining its grid.")
    if not isinstance(values, Mapping) or set(values) - set(DEFAULT_GRID):
        raise ValueError("The hyperparameter grid contains an unsupported parameter.")

    def numeric(key, **kwargs):
        """

        Reads one numerical parameter list using its entered value or packaged preset.

        Args:
            key: Grid parameter name.
            kwargs: Integer and range checks passed to grid_values().

        Returns:
            The parsed values for that parameter.
        """

        return grid_values(values.get(key, DEFAULT_GRID[key]), name=key.replace("_", " "), **kwargs)

    if method.endswith("rtrees"):
        names = ("max_depth", "min_sample_count", "active_var_count", "max_trees")
        axes = [
            numeric(
                "rtrees_" + name,
                integer=True,
                minimum=0,
                positive=name in {"max_depth", "min_sample_count", "max_trees"},
            )
            for name in names
        ]
        count = math.prod(map(len, axes))
        if count > MAX_CANDIDATES:
            raise ValueError(
                f"The grid contains {count} combinations. Reduce it to at most {MAX_CANDIDATES} candidates."
            )
        rows = [{"rtrees": dict(zip(names, items))} for items in product(*axes)]
    elif method == "logistic_regression":
        raw = values.get(
            "logistic_regression_regularizations", DEFAULT_GRID["logistic_regression_regularizations"]
        )
        if not isinstance(raw, str) or len(raw) > 40:
            raise ValueError("Logistic regularization: enter L2, DISABLE, or both separated by a comma.")
        regularizations = [part.strip().upper() for part in raw.split(",")]
        if (
            not 1 <= len(regularizations) <= 2
            or len(set(regularizations)) != len(regularizations)
            or any(item not in {"L2", "DISABLE"} for item in regularizations)
        ):
            raise ValueError("Logistic regularization: enter L2, DISABLE, or both without repeats.")
        rates = numeric("logistic_regression_learning_rate", positive=True)
        iterations = numeric("logistic_regression_iterations", integer=True, positive=True)
        count = len(regularizations) * len(rates) * len(iterations)
        if count > MAX_CANDIDATES:
            raise ValueError(
                f"The grid contains {count} combinations. Reduce it to at most {MAX_CANDIDATES} candidates."
            )
        rows = [
            {"logistic_regression": {
                "regularization": regularization, "learning_rate": rate, "iterations": count
            }}
            for regularization, rate, count in product(regularizations, rates, iterations)
        ]
    else:
        raw = values.get("svm_kernels", DEFAULT_GRID["svm_kernels"])
        if not isinstance(raw, str) or len(raw) > 40:
            raise ValueError("SVM kernels: enter LINEAR, RBF, or both separated by a comma.")
        kernels = [part.strip().upper() for part in raw.split(",")]
        if (
            not 1 <= len(kernels) <= 2
            or len(set(kernels)) != len(kernels)
            or any(k not in {"LINEAR", "RBF"} for k in kernels)
        ):
            raise ValueError("SVM kernels: enter LINEAR, RBF, or both without repeats.")
        cs = numeric("svm_c", positive=True)

        # Pair each C with a linear candidate and with every requested RBF gamma.
        gammas = numeric("svm_gamma", positive=True) if "RBF" in kernels else [None]
        count = len(cs) * sum(len(gammas) if k == "RBF" else 1 for k in kernels)
        if count > MAX_CANDIDATES:
            raise ValueError(
                f"The grid contains {count} combinations. Reduce it to at most {MAX_CANDIDATES} candidates."
            )
        rows = [
            {"svm": {"kernel": kernel, "c": c, "gamma": gamma}}
            for kernel in kernels
            for c in cs
            for gamma in (gammas if kernel == "RBF" else [None])
        ]
    if graph_widths and method in {"kronos_rtrees", "kronos_svm"}:
        widths = grid_values(
            graph_widths, name="Spatial feature widths", integer=True, positive=True
        )
        if len(rows) * len(widths) > MAX_CANDIDATES:
            raise ValueError(
                f"The grid contains {len(rows)*len(widths)} combinations including spatial widths. Reduce it to at most {MAX_CANDIDATES} candidates."
            )
        rows = [
            {**copy.deepcopy(row), "graph": {"hidden_width": width}}
            for width in widths
            for row in rows
        ]
    return rows


def expand_grid(base: dict, values: Mapping[str, object], *, graph_widths=None) -> list[dict]:
    """

    Creates a complete model configuration for every entered parameter combination.

    Copy the base settings for each candidate, then replace only parameters included
    in the grid. For KRONOS, classifier parameters are updated inside its native-model
    settings and feature dimensions in its graph settings. Validate the completed
    configurations together before returning them for fitting.

    Args:
        base: Complete settings for the selected conventional or KRONOS classifier.
        values: Grid parameter lists to compare.
        graph_widths: Optional GNN feature dimensions for a KRONOS search.

    Returns:
        The fully specified and validated candidate configurations.

    Raises:
        ValueError: The grid or a completed model configuration is invalid.
    """

    from .cross_validation import QuPathGroupedCVConfiguration

    method = {
        "qupath-pixel-rtrees/v1": "rtrees",
        "qupath-pixel-svm/v1": "svm",
        "qupath-object-rtrees/v1": "rtrees",
        "qupath-object-svm/v1": "svm",
        "qupath-object-logistic-regression/v1": "logistic_regression",
        "qupath-kronos-rtrees/v2": "kronos_rtrees",
        "qupath-kronos-svm/v2": "kronos_svm",
    }.get(base.get("configuration_schema"))
    rows = []
    for changes in parameter_combinations(method, values, graph_widths=graph_widths):

        # Copy the complete settings before inserting each candidate’s parameter values.
        candidate = copy.deepcopy(base)
        native = candidate.get("native_configuration", candidate)
        for family, parameters in changes.items():
            target = candidate["graph"] if family == "graph" else native[family]
            target.update(parameters)
        rows.append(candidate)
    checked = QuPathGroupedCVConfiguration.from_mapping(
        {
            "configuration_schema": "qupath-grouped-cross-validation/v1",
            "folds": 0,
            "scoring": "subject_weighted_macro_f1/v1",
            "candidates": rows,
        }
    )
    return checked.to_mapping()["candidates"]
