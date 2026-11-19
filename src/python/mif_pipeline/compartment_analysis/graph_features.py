"""

Stores the fitted mean-self graph matrices and bias.

We retain finite float32 self and neighbour weights with shape (D, H), together
with the H-element bias, in immutable arrays. Python graph fitting returns these
parameters for native spatial-feature application through GraphHeadKernel.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .patch_graph import GraphInputError

# ==============================================================================
#                                 Graph features
# ==============================================================================


def _frozen_array(value: np.ndarray, name: str, ndim: int) -> np.ndarray:
    """

    Checks a learned parameter array and stores an independent read-only float32 copy.

    We copy through immutable bytes so later changes to the supplied training
    array cannot change a saved model's weights during prediction.

    Args:
        value: Learned parameter array to retain.
        name: Parameter name included in an error.
        ndim: Required number of array dimensions.

    Returns:
        Read-only float32 array with the checked shape and values.

    Raises:
        GraphInputError: The array has the wrong dtype/dimensions, an empty
            dimension or a NaN/infinite value.
    """

    if not isinstance(value, np.ndarray) or value.dtype != np.float32 or value.ndim != ndim:
        raise GraphInputError(f"{name} must be a float32 {ndim}D array")
    if not all(value.shape) or not np.isfinite(value).all():
        raise GraphInputError(f"{name} must be nonempty and finite")
    return np.frombuffer(np.ascontiguousarray(value).tobytes(), dtype=np.float32).reshape(
        value.shape
    )


@dataclass(frozen=True)
class FrozenMeanSelf:
    """

    Stores the trained weights used to combine self and neighbour encoder features.

    For encoder dimension D and output dimension H, both weight matrices have
    shape (D, H). Their products are added with one H-element bias before ReLU.
    We retain these trained parameters without updating them during prediction.

    Attributes:
        self_weight: Weights applied to the target patch's encoder vector.
        neighbour_weight: Weights applied to its mean neighbour encoder vector.
        bias: Additive value for each output feature.
    """

    self_weight: np.ndarray = field(repr=False)
    neighbour_weight: np.ndarray = field(repr=False)
    bias: np.ndarray = field(repr=False)

    def __post_init__(self) -> None:
        """

        Checks the learned matrix dimensions and retains independent read-only parameters.

        Both matrices must describe the same input and output dimensions, and the
        bias length must equal the output dimension. All parameters must be finite
        float32 arrays before they are accepted for prediction.

        Raises:
            GraphInputError: Shapes, dtypes or numerical values are incompatible.
        """

        own = _frozen_array(self.self_weight, "self_weight", 2)
        neighbour = _frozen_array(self.neighbour_weight, "neighbour_weight", 2)
        bias = _frozen_array(self.bias, "bias", 1)
        if own.shape != neighbour.shape or own.shape[1] != bias.shape[0]:
            raise GraphInputError("frozen parameter shapes are incompatible")
        object.__setattr__(self, "self_weight", own)
        object.__setattr__(self, "neighbour_weight", neighbour)
        object.__setattr__(self, "bias", bias)


