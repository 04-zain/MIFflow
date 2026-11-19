"""Shared explanations of the purpose and effect of processing settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

CONTROL_HELP = {
    "solver": "Estimates the contribution of each fluorophore to the measured signal.",
    "spectral_state_override": "Choose Yes for mixed fluorescence measurements. Choose the appropriate No option for prior unmixing or signals isolated by measurement bands. Both No options preserve the image intensities.",
    "chunk_width": "Width of each block of pixels processed together. Smaller blocks require less memory.",
    "chunk_height": "Height of each block of pixels processed together. Smaller blocks require less memory.",
    "calculation_precision": "Choose the precision used for spectral unmixing calculations. Methods that require a specific precision use it automatically.",
    "output_dtype": "Choose the precision used to store fluorescence intensities. 64-bit values require twice the storage of 32-bit values.",
    "nnls_tolerance": "Sets the tolerance used to decide whether the NNLS fit has converged. A smaller tolerance can require more iterations.",
    "nnls_max_iterations": "Maximum number of updates used to fit the fluorescence contributions in each processing block.",
    "overwrite": "When repeating unmixing, replace the processed image already saved at the selected output path with the new result. The original scanned image remains unchanged.",
    "normalization": "Rescales the classifier features before fitting.",
    "random_seed": "Sets the starting value for reproducible random choices during model fitting.",
    "seed": "Sets the starting value for reproducible random choices.",
    "threads": "Number of processor threads available to this step. More threads can increase processing speed and memory use.",
}

# ==============================================================================
#                                Control guidance
# ==============================================================================


def control_help(name: str, supplied: str | None = None) -> str | None:
    if name == "normalization" and supplied:
        return supplied
    if name in CONTROL_HELP:
        return CONTROL_HELP[name]
    if supplied:
        return supplied
    for suffix in ("normalization", "random_seed", "seed", "threads", "overwrite"):
        if name.endswith("_" + suffix):
            return CONTROL_HELP[suffix]
    return None


RF_PARAMETER_HELP = {
    "max_depth": "Limits the number of successive splits along a tree branch.",
    "min_sample_count": "Stops a node from splitting when it contains this many training observations or fewer.",
    "active_var_count": "Number of randomly selected features considered at each split. The default uses the square root of the number of input features, rounded to a whole number.",
    "max_trees": "Limits the number of trees whose votes determine the predicted class.",
    "termination_epsilon": "Stop adding trees when the out-of-bag error falls below this value. Set 0 to fit the maximum number of trees.",
    "calculate_variable_importance": "Calculate feature importance from the fitted forest.",
}


CLASSIFIER_SCALING_HELP = {
    "none": "",
    "mean_variance": "Subtracts each feature's training mean and divides by its standard deviation.",
    "min_max": "Maps each feature's training minimum to 0 and maximum to 1.",
}
