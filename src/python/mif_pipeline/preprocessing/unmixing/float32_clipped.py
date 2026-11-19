"""

Performs clipped least squares using float32 pixel calculations on CPU, CUDA or Metal.

The small spectral matrix is prepared in float64. Its inverse is converted to
float32 and screened for rounding sensitivity before processing pixels. Each
regional block is multiplied by that transform and negative coefficients are
clipped to zero. Results use float64 storage to match the shared solver interface;
their calculation retains float32 accuracy.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from threading import Lock
import numpy as np
from .solvers import (
    SolverError,
    SolverResult,
    SolverDiagnostics,
    _validated_spectral_matrix,
    _validated_signals,
)

# ==============================================================================
#                                Float32 clipped
# ==============================================================================


class Float32ClippedLeastSquaresSolver:
    """

    Keeps a prepared float32 transform for successive clipped-least-squares pixel blocks.

    Attributes:
        matrix: Checked float64 reference matrix of shape (bands, components).
        transform: Float32 pseudoinverse of shape (components, bands).
        condition_number: Largest divided by smallest reference-matrix singular value.
        roundoff_estimate: Combined matrix-level estimate of conversion and multiplication error.
        roundoff_budget: Maximum accepted preparation estimate, initially 1e-4.
        execution_device: Selected CPU, CUDA or Metal processor.
        execution_precision_policy: Recorded "float32_clipped" calculation route.
        working_pixels_per_block: Maximum pixel columns in one internal block.
        refined_pixels: Number of float64 refits; zero for this implementation.
    """

    working_pixels_per_block = 65536
    execution_precision_policy = "float32_clipped"
    roundoff_budget = 1e-4
    refined_pixels = 0

    def __init__(self, matrix, *, device="cpu"):
        """

        Prepares the inverse and checks whether the spectral matrix is suitable for float32 fitting.

        Calculate its pseudoinverse and singular values in float64. Converting the inverse
        must preserve finite values and must not erase nonzero entries. Estimate the effects
        of rounding the inverse, rounding measurements and performing dot products; reject
        the route when their combined matrix-level estimate exceeds its budget.

        Args:
            matrix: Full-column-rank band-by-component reference matrix.
            device: "cpu", "cuda" or "mps" for Metal.

        Raises:
            SolverError: Matrix preparation, conversion, processor settings or the
                estimated float32 rounding sensitivity are unsuitable.
        """

        if device not in {"cpu", "mps", "cuda"}:
            raise SolverError("Unsupported clipped-LS processor")
        self.matrix = _validated_spectral_matrix(matrix)

        # The spectral matrix is small and reused for the entire image.
        # Prepare its inverse at higher precision before reducing the pixel work.
        transform = np.linalg.pinv(self.matrix)
        singular = np.linalg.svd(self.matrix, compute_uv=False)
        self.condition_number = float(singular[0] / singular[-1])
        with np.errstate(over="ignore", under="ignore", invalid="ignore"):
            reduced = transform.astype(np.float32)
        if not np.isfinite(reduced).all() or np.any((transform != 0) & (reduced == 0)):
            raise SolverError("This spectral basis requires 64-bit calculation; choose float64")
        u = np.finfo(np.float32).eps / 2
        bands = self.matrix.shape[0]
        gamma = bands * u / (1 - bands * u)

        # Combine inverse-conversion, input-rounding and multiplication estimates, then scale by
        # ||S||.
        self.roundoff_estimate = float(
            (
                np.linalg.norm(transform - reduced.astype(np.float64), 2)
                + u * np.linalg.norm(reduced.astype(np.float64), 2)
                + gamma * (1 + u) * np.linalg.norm(np.abs(reduced.astype(np.float64)), 2)
            )
            * singular[0]
        )
        if not np.isfinite(self.roundoff_estimate) or self.roundoff_estimate > self.roundoff_budget:
            raise SolverError(
                "These reference spectra are too numerically sensitive for 32-bit clipped LS; choose float64"
            )
        self.execution_device = device
        self.transform = reduced
        self.transform.setflags(write=False)
        self._lock = Lock()
        self._torch = None
        if device != "cpu":
            import torch

            # Disable reduced-mantissa CUDA multiplication for the requested float32
            # calculation.
            if device == "cuda" and torch.backends.cuda.matmul.allow_tf32:
                raise SolverError(
                    "Disable reduced-mantissa CUDA multiplication for 32-bit spectral calculations"
                )
            self._torch = torch
            self._device_transform = torch.tensor(
                self.transform.copy(), dtype=torch.float32, device=device
            )

    def solve(self, signals):
        """

        Applies the prepared float32 transform to successive pixel blocks and clips negative outputs.

        Check each input block's conversion to float32, perform the multiplication on the
        chosen processor, and reject nonfinite fitted values. Copy the resulting coefficients
        into a combined component-by-pixel array in the original pixel order.

        Args:
            signals: Finite normalized measurements with shape (bands, pixels).

        Returns:
            A SolverResult containing the float32-calculated coefficients in float64
            storage and clipped-LS completion diagnostics.

        Raises:
            SolverError: Input dimensions, float32 conversion, block size or fitted values are invalid.
        """

        values = _validated_signals(signals, self.matrix.shape[0])
        if type(self.working_pixels_per_block) is not int or self.working_pixels_per_block <= 0:
            raise SolverError("Processing block size must be positive")
        output = np.empty((self.matrix.shape[1], values.shape[1]), dtype=np.float64)
        with self._lock:
            for start in range(0, values.shape[1], self.working_pixels_per_block):
                block = values[:, start : start + self.working_pixels_per_block]
                with np.errstate(over="ignore", under="ignore", invalid="ignore"):
                    working = np.ascontiguousarray(block, dtype=np.float32)
                if not np.isfinite(working).all() or np.any((block != 0) & (working == 0)):
                    raise SolverError("The image intensity range requires 64-bit processing")
                if self._torch is None:
                    with np.errstate(over="ignore", invalid="ignore"):
                        fitted = self.transform @ working
                else:
                    torch = self._torch
                    with (
                        torch.inference_mode(),
                        torch.autocast(self.execution_device, enabled=False),
                    ):
                        pixels = torch.as_tensor(
                            working, dtype=torch.float32, device=self.execution_device
                        )
                        fitted = (self._device_transform @ pixels).cpu().numpy()
                if not np.isfinite(fitted).all():
                    raise SolverError("Nonfinite 32-bit estimate; use float64 processing")
                np.maximum(fitted, 0, out=fitted)

                # The shared interface stores float64, but these values
                # retain the accuracy of the float32 calculation above.
                output[:, start : start + block.shape[1]] = fitted
        return SolverResult(
            output, SolverDiagnostics(values.shape[1], values.shape[1], 0, 0, True, 0.0)
        )
