"""

Selects a spectral solver and supplies the float64 PyTorch implementation.

Selection combines the requested estimator, tolerance, precision and processor.
Float32 NNLS is used only when its minimum supported tolerance is satisfied.
Float64 CUDA calculations use the tensor implementation here; ordinary float64
CPU calculations use the NumPy solver.

The float64 tensor route checks its candidate results against the original
reference matrix and refits rejected columns with the corresponding CPU solver.
The separate float32 implementations follow their own stopping rules.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from threading import Lock
import math
import os
import numpy as np
from .solvers import (
    SolverResult,
    SolverDiagnostics,
    SolverError,
    BatchedNnlsSolver,
    ClippedLeastSquaresSolver,
    _validated_spectral_matrix,
    _validated_signals,
)

PRECISION_POLICIES = frozenset({"float64"})

# ==============================================================================
#                                 Tensor solver
# ==============================================================================


class TensorSpectralSolver:
    """

    Keeps matching CPU and PyTorch solvers for float64 spectral fitting and result checks.

    Attributes:
        matrix: Original float64 reference matrix.
        kind: Requested "nnls" or "clipped_ls" estimator.
        device: PyTorch device used for fitting.
        execution_device: Processor name retained in the execution report.
        precision_policy: Supported "float64" setting.
        execution_precision_policy: Numerical width recorded with the result.
        tolerance: Requested NNLS tolerance used for tensor and CPU checks.
        max_iterations: Maximum tensor-coordinate sweeps.
        working_pixels_per_block: Maximum pixel columns handled per block.
        cpu: Prepared CPU solver used to refit rejected candidates.
        s: Float64 reference matrix on the selected tensor device.
        transform: Pseudoinverse on the same device.
        gram: S.T @ S used during coordinate updates.
        refined_pixels: Cumulative columns refitted on the CPU.
        verified_pixels: Cumulative columns checked after tensor calculation.
    """

    working_pixels_per_block = 65536

    def __init__(
        self,
        matrix,
        *,
        kind,
        device="cuda",
        tolerance=1e-8,
        max_iterations=500,
        precision_policy="float64",
    ):
        """

        Prepares the CPU reference calculations and transfers matching float64 matrices to PyTorch.

        Build the requested CPU estimator first, then copy its matrix and pseudoinverse
        to the chosen device. Calculate S.T @ S for coordinate updates and check that
        its values and component diagonals remain usable.

        Args:
            matrix: Finite full-column-rank reference matrix.
            kind: "nnls" or "clipped_ls".
            device: "cuda" or "cpu" for this float64 implementation.
            tolerance: Positive requested NNLS tolerance.
            max_iterations: Positive tensor-sweep limit.
            precision_policy: "float64".

        Raises:
            SolverError: Estimator, device, precision, matrix or fitting settings are unsupported.
        """

        if precision_policy not in PRECISION_POLICIES:
            raise SolverError("Mixed-precision unmixing is retired; use float64")
        if device == "mps":
            raise SolverError(
                "64-bit NNLS is not supported on Metal; choose 32-bit clipped least squares or CPU processing"
            )
        if device not in {"cuda", "cpu"}:
            raise SolverError("Unsupported spectral device")
        if kind not in {"nnls", "clipped_ls"}:
            raise SolverError("Unknown spectral solver")
        if isinstance(tolerance, bool) or not math.isfinite(tolerance) or tolerance <= 0:
            raise SolverError("Spectral tolerance must be finite and positive")
        if type(max_iterations) is not int or max_iterations < 1:
            raise SolverError("Spectral maximum iterations must be a positive integer")
        self.matrix = _validated_spectral_matrix(matrix)
        self.kind, self.device = kind, device
        self.precision_policy = self.execution_precision_policy = precision_policy
        self.tolerance, self.max_iterations = float(tolerance), max_iterations
        self.execution_device = device
        self._lock = Lock()
        self.refined_pixels = self.verified_pixels = 0
        self.cpu = (
            BatchedNnlsSolver(self.matrix, tolerance=tolerance, max_iterations=max_iterations)
            if kind == "nnls"
            else ClippedLeastSquaresSolver(self.matrix)
        )
        self._pinv = self.cpu.transform
        import torch

        self.torch = torch
        self._dtype = torch.float64

        # No implicit framework fallback, autocast or half-precision tensor is enabled.
        self.s = torch.tensor(self.matrix, dtype=self._dtype, device=device)
        self.transform = torch.tensor(self._pinv, dtype=self._dtype, device=device)
        self.gram = self.s.T @ self.s
        self._matrix_safe = bool(
            torch.isfinite(self.s).all()
            & torch.isfinite(self.transform).all()
            & torch.isfinite(self.gram).all()
            & (self.gram.diag() > 0).all()
        )

    def _gradient(self, x, block):
        """

        Checks a candidate against the original spectral model using float64 NumPy calculations.

        Compute S @ X - Y, then multiply by S.T and retain gradient entries that permit
        an improvement under the nonnegative constraint. The allowed error is scaled by
        tolerance × (1 + max(abs(S.T @ Y))) for each pixel.

        Args:
            x: Candidate coefficients with shape (components, pixels).
            block: Original normalized measurements with shape (bands, pixels).

        Returns:
            Per-pixel projected-gradient magnitudes and their allowed thresholds.
        """

        with np.errstate(over="ignore", invalid="ignore"):
            rhs = self.matrix.T @ block
            g = self.matrix.T @ (self.matrix @ x - block)
            norm = np.max(np.abs(np.where((x > 0) | (g < 0), g, 0)), axis=0)
        return norm, self.tolerance * (1 + np.max(np.abs(rhs), axis=0))

    def _candidate(self, block):
        """

        Generates a tensor fit and, for NNLS, updates unresolved coefficients by coordinate sweeps.

        Start from the prepared inverse. NNLS clips the initial result and iteratively
        updates components until its gradient check passes or the sweep limit is reached.
        The enclosing solve() checks these candidates against the original reference.

        Args:
            block: Float64 normalized measurement block.

        Returns:
            Candidate coefficients, the initially unresolved NNLS count and completed sweeps.
        """

        t = self.torch
        if not self._matrix_safe:
            return np.full((self.matrix.shape[1], block.shape[1]), np.nan), block.shape[1], 0
        y = t.as_tensor(np.ascontiguousarray(block), dtype=self._dtype, device=self.device)
        if not bool(t.isfinite(y).all()):
            return np.full((self.matrix.shape[1], block.shape[1]), np.nan), block.shape[1], 0
        x = self.transform @ y
        if self.kind == "clipped_ls":
            return x.cpu().numpy().astype(np.float64, copy=True), 0, 0
        x.clamp_(min=0)
        rhs = self.s.T @ y
        candidate_tol = self.tolerance
        cutoff = candidate_tol * (1 + rhs.abs().amax(dim=0))

        def pending_columns():
            """

            Identifies pixels whose current tensor coefficients still exceed the NNLS gradient threshold.

            Returns:
                A boolean vector selecting columns with nonfinite or excessive gradient values.
            """

            g = self.gram @ x - rhs
            residual = t.where((x > 0) | (g < 0), g, 0).abs().amax(dim=0)
            return (~t.isfinite(residual)) | (residual > cutoff)

        pending = pending_columns()
        constrained, completed = int(pending.sum().item()), 0
        limit = self.max_iterations
        for step in range(limit):
            if not bool(pending.any()):
                break
            for k in range(self.matrix.shape[1]):
                value = (rhs[k] - self.gram[k] @ x + self.gram[k, k] * x[k]) / self.gram[k, k]
                x[k] = t.where(pending, value.clamp(min=0), x[k])
            completed = step + 1
            pending = pending_columns()
        return x.cpu().numpy().astype(np.float64, copy=True), constrained, completed

    def solve(self, signals):
        """

        Fits regional spectra with tensors and refits rejected pixel columns on the CPU.

        For NNLS, accept tensor candidates only when they are finite, nonnegative and
        within the original model's gradient tolerance. Send the remaining columns to the
        matching CPU estimator and place them back at their original positions. Final
        clipping and gradient checks determine the returned convergence report.

        Args:
            signals: Finite normalized band-by-pixel measurements.

        Returns:
            Component coefficients and diagnostics combining tensor work and CPU refits.

        Raises:
            SolverError: Inputs are invalid or coefficients remain nonfinite after CPU refitting.
        """

        values = _validated_signals(signals, self.matrix.shape[0])
        if type(self.working_pixels_per_block) is not int or self.working_pixels_per_block < 1:
            raise SolverError("Spectral working block must be a positive integer")
        output = np.empty((self.matrix.shape[1], values.shape[1]), dtype=np.float64)
        constrained = iterations = 0
        maximum, converged = 0.0, True
        with self._lock, self.torch.inference_mode():
            for start in range(0, values.shape[1], self.working_pixels_per_block):
                block = values[:, start : start + self.working_pixels_per_block]
                solved, n_constrained, steps = self._candidate(block)
                constrained += n_constrained
                iterations = max(iterations, steps)
                if self.kind == "nnls":
                    norm, cutoff = self._gradient(solved, block)
                    accepted = (
                        np.isfinite(solved).all(axis=0)
                        & (solved >= 0).all(axis=0)
                        & (norm <= cutoff)
                    )
                else:
                    accepted = np.isfinite(solved).all(axis=0)

                # Preserve valid tensor results and refit only rejected
                # columns with the same estimator at float64 on the CPU.
                repair = ~accepted
                if repair.any():
                    fixed = self.cpu.solve(block[:, repair])
                    solved[:, repair] = fixed.coefficients
                    self.refined_pixels += int(repair.sum())
                    converged &= fixed.diagnostics.converged
                    iterations = max(iterations, fixed.diagnostics.iterations)
                self.verified_pixels += block.shape[1]
                np.maximum(solved, 0.0, out=solved)
                if not np.isfinite(solved).all():
                    raise SolverError("Spectral result is nonfinite after float64 refinement")
                if self.kind == "nnls":
                    norm, cutoff = self._gradient(solved, block)
                    converged &= bool(np.all(np.isfinite(norm) & (norm <= cutoff)))
                    maximum = max(maximum, float(norm.max(initial=0)))
                output[:, start : start + solved.shape[1]] = solved
        return SolverResult(
            output,
            SolverDiagnostics(
                values.shape[1],
                values.shape[1] - constrained,
                constrained,
                iterations,
                converged,
                maximum,
            ),
        )


def create_spectral_solver(
    matrix, kind, *, tolerance=1e-8, max_iterations=500, preference=None, precision_policy=None
):
    """

    Selects the numerical implementation for the requested spectral method and accuracy.

    Combine the explicit settings with the configured processor preference. NNLS can
    use float32 when that width is requested and tolerance is at least 1e-5; tighter
    requests retain float64. Metal uses the supported float32 routes, while a float64
    request with a Metal preference is directed to CPU.

    The returned solver records its actual processor and precision so the analysis
    can report the implementation that ran.

    Args:
        matrix: Band-by-component reference matrix to prepare.
        kind: "nnls" or "clipped_ls" estimator.
        tolerance: Requested NNLS stopping tolerance.
        max_iterations: Requested NNLS iteration budget.
        preference: Processor preference, or None to use the application/environment choice.
        precision_policy: Requested numerical width, or None to use the configured policy.

    Returns:
        A prepared solver with solve(), execution_device and execution_precision_policy.

    Raises:
        SolverError: The estimator, precision, tolerance or prepared matrix is invalid.
    """

    from mif_pipeline._devices import select_torch_device

    if kind not in {"nnls", "clipped_ls"}:
        raise SolverError("Unknown spectral solver")
    from mif_pipeline._compute_preferences import preferred_precision

    selected_precision = preferred_precision() if precision_policy is None else None
    requested = precision_policy or os.environ.get("MIF_SPECTRAL_PRECISION", "device_selected")
    if (
        precision_policy is None
        and requested == "device_selected"
        and selected_precision is not None
    ):
        requested = "float32_clipped" if selected_precision == "float32" else "float64"
    if requested == "float32":
        requested = "float32_clipped"
    if requested not in {"device_selected", "float64", "float32_clipped", "float32_nnls"}:
        raise SolverError(
            "This saved precision policy is no longer supported. Choose the calculation precision in Unmixing Advanced settings."
        )
    from .float32_nnls import Float32NnlsSolver

    chosen = preference or os.environ.get("MIF_COMPUTE_DEVICE", "auto")
    wants32 = requested in {"float32_clipped", "float32_nnls"} or (
        requested == "device_selected" and chosen == "mps"
    )
    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not math.isfinite(tolerance)
        or tolerance <= 0
    ):
        raise SolverError("NNLS tolerance must be finite and positive.")

    # Use float64 when the requested NNLS tolerance is tighter than the float32 route supports.
    nnls32 = kind == "nnls" and wants32 and tolerance >= Float32NnlsSolver.minimum_tolerance
    permitted = (
        "float32_nnls"
        if nnls32
        else "float32_clipped" if kind == "clipped_ls" and requested != "float64" else "float64"
    )

    # Resolve a float64/Metal request to CPU before constructing the solver.
    if chosen == "mps" and permitted == "float64":
        chosen = "cpu"
    decision = select_torch_device(
        "unmixing", preference=chosen, required_bytes=256 * 1024 * 1024, precision_policy=permitted
    )
    precision = (
        "float32_nnls"
        if nnls32
        else (
            "float32_clipped"
            if kind == "clipped_ls" and (wants32 or decision.device == "mps")
            else "float64"
        )
    )
    if precision == "float32_nnls":
        result = Float32NnlsSolver(
            matrix, device=decision.device, tolerance=tolerance, max_iterations=max_iterations
        )
    elif precision == "float32_clipped":
        from .float32_clipped import Float32ClippedLeastSquaresSolver

        result = Float32ClippedLeastSquaresSolver(matrix, device=decision.device)
    elif decision.device == "cuda":
        result = TensorSpectralSolver(
            matrix, kind=kind, device="cuda", tolerance=tolerance, max_iterations=max_iterations
        )
    else:
        result = (
            ClippedLeastSquaresSolver(matrix)
            if kind == "clipped_ls"
            else BatchedNnlsSolver(matrix, tolerance=tolerance, max_iterations=max_iterations)
        )
        result.execution_precision_policy = "float64"
    result.execution_device = decision.device
    result.execution_reason = (
        "NNLS retains the requested stopping tolerance using 64-bit CPU calculation. 32-bit NNLS is available for a tolerance of 0.00001 or greater."
        if kind == "nnls"
        and not nnls32
        and (preference or os.environ.get("MIF_COMPUTE_DEVICE")) == "mps"
        else decision.reason
    )
    return result
