"""

Fits nonnegative component amounts through float32 coordinate updates.

Starting from clipped least squares, update one component at a time and adjust
its contribution to the reconstructed spectrum. Recompute the full residual and
gradient after each sweep to decide which pixels need further work. Small libraries
can also try prepared component subsets within the remaining iteration budget.

This route requires a requested tolerance of at least 1e-5. Tighter requirements
are routed to float64 by the solver selector. Output arrays use the shared float64
storage interface, while the pixel calculations and stopping checks use float32.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from contextlib import nullcontext
from threading import Lock
import math
import numpy as np
from .float32_clipped import Float32ClippedLeastSquaresSolver
from .solvers import SolverError, SolverResult, SolverDiagnostics, _validated_signals

# ==============================================================================
#                                  Float32 nnls
# ==============================================================================


class Float32NnlsSolver:
    """

    Stores the float32 matrices and settings reused during regional nonnegative fitting.

    Attributes:
        minimum_tolerance: Smallest accepted requested tolerance, 1e-5.
        working_pixels_per_block: Maximum pixel columns handled by one internal block.
        execution_precision_policy: Recorded "float32_nnls" route.
        execution_device: Selected CPU, CUDA or Metal processor.
        matrix: Checked reference matrix retained in float64.
        transform: Float32 inverse used for the initial clipped fit.
        tolerance: Requested per-pixel stopping tolerance.
        max_iterations: Coordinate-sweep and subset-attempt budget.
        active_transforms: Prepared transforms for nonempty subsets when there are
            at most eight components; otherwise None.
        s: Reference matrix on the chosen processor in float32.
        diagonal: Squared reference-column norms used to scale coordinate updates.
        refined_pixels: Float64-refit count, zero for this implementation.
        verified_pixels: Separate float64-verification count, also zero for this route.
    """

    minimum_tolerance = 1e-5
    working_pixels_per_block = 65536
    execution_precision_policy = "float32_nnls"
    refined_pixels = 0
    verified_pixels = 0

    def __init__(self, matrix, *, device="cpu", tolerance=1e-5, max_iterations=500):
        """

        Prepares the initial transform, coordinate scales and small-system subset transforms.

        Reuse the clipped-LS matrix screening, then calculate each reference column's
        squared norm. For at most eight components, prepare transforms for all nonempty
        subsets, with zero output rows for omitted components. Transfer working matrices
        to the requested processor before pixel fitting begins.

        Args:
            matrix: Finite full-column-rank reference array with bands in rows.
            device: "cpu", "cuda" or "mps".
            tolerance: Requested stopping tolerance of at least 1e-5.
            max_iterations: Positive fitting iteration budget.

        Raises:
            SolverError: Matrix, precision range, processor or numerical settings are invalid.
        """

        if (
            isinstance(tolerance, bool)
            or not isinstance(tolerance, (int, float))
            or not math.isfinite(tolerance)
            or tolerance < self.minimum_tolerance
        ):
            raise SolverError(
                "32-bit NNLS requires a stopping tolerance of 0.00001 or greater; tighter tolerances use 64-bit calculation."
            )
        if type(max_iterations) is not int or max_iterations < 1:
            raise SolverError("NNLS iteration limit must be a positive integer.")
        self.prepared = Float32ClippedLeastSquaresSolver(matrix, device=device)
        self.matrix = self.prepared.matrix
        self.transform = self.prepared.transform
        self.tolerance = float(tolerance)
        self.max_iterations = max_iterations
        self.execution_device = device
        self._torch = self.prepared._torch
        self._lock = Lock()
        self.active_transforms = None
        components = self.matrix.shape[1]
        if components <= 8:
            transforms = []
            for mask in sorted(range(1, 1 << components), key=lambda m: (-m.bit_count(), m)):
                indices = [i for i in range(components) if mask & (1 << i)]
                transform = np.zeros_like(self.transform)
                transform[indices] = np.linalg.pinv(self.matrix[:, indices]).astype(np.float32)
                transforms.append(transform)
            self.active_transforms = np.stack(transforms)
        self.s = np.asarray(self.matrix, dtype=np.float32)
        self.diagonal = np.sum(self.s * self.s, axis=0)
        if not np.isfinite(self.diagonal).all() or np.any(self.diagonal <= 0):
            raise SolverError("The reference range requires 64-bit NNLS.")
        if self._torch is not None:
            t = self._torch
            self.s = t.tensor(self.s, dtype=t.float32, device=device)
            self.diagonal = t.tensor(self.diagonal, dtype=t.float32, device=device)
            if self.active_transforms is not None:
                self.active_transforms = t.tensor(
                    self.active_transforms, dtype=t.float32, device=device
                )

    def _block(self, values):
        """

        Fits one block by updating component coefficients and their spectral residuals together.

        For residual R = S @ X - Y, changing coefficient j by delta changes the residual
        by S[:, j] × delta. This avoids rebuilding the complete spectrum after every
        coordinate. After a sweep, recompute the full residual and projected gradient.
        Pixels meeting the stopping test receive no further coordinate changes.

        The final threshold is tolerance × max(abs(S.T @ Y)) per pixel. Internal updates
        use half that threshold as a working target. With cached subset transforms, at
        most sixteen sweeps precede subset trials within the remaining budget.

        Args:
            values: Normalized band-by-pixel block to convert to float32 and fit.

        Returns:
            Coefficients, the initially unresolved pixel count, iteration count,
            convergence flag and largest projected-gradient value.

        Raises:
            SolverError: Float32 conversion or subsequent calculations produce invalid values.
        """

        with np.errstate(over="ignore", under="ignore", invalid="ignore"):
            y = np.ascontiguousarray(values, dtype=np.float32)
        if not np.isfinite(y).all() or np.any((values != 0) & (y == 0)):
            raise SolverError("The image intensity range requires 64-bit NNLS.")
        t = self._torch
        s = self.s
        if t is None:
            maximum = np.maximum
            absolute = np.abs
            reduce = lambda a: np.max(a, axis=0)
            where = np.where
            finite = lambda a: bool(np.isfinite(a).all())
            x = maximum(self.transform @ y, 0)
        else:
            y = t.tensor(y, dtype=t.float32, device=self.execution_device)
            maximum = lambda a, b: a.clamp(min=b)
            absolute = t.abs
            reduce = lambda a: a.amax(dim=0)
            where = t.where
            finite = lambda a: bool(t.isfinite(a).all())
            x = maximum(self.prepared._device_transform @ y, 0)
        rhs = s.T @ y

        # Scale the requested tolerance by each pixel’s largest projected measurement magnitude.
        cutoff = self.tolerance * reduce(absolute(rhs))

        def check():
            """

            Recalculates the full spectral residual and remaining gradient violation for each pixel.

            At positive coefficients both gradient signs matter. At zero, a negative gradient
            indicates an allowed increase that could improve the fit. Only those entries
            contribute to the stopping measurement.

            Returns:
                The residual array and each pixel's largest absolute projected-gradient value.
            """

            residual = s @ x - y
            gradient = s.T @ residual
            norm = reduce(absolute(where((x > 0) | (gradient < 0), gradient, 0)))
            return residual, norm

        residual, norm = check()
        if not finite(rhs) or not finite(norm) or not finite(cutoff):
            raise SolverError("The signal magnitude requires 64-bit NNLS.")
        pending = norm > cutoff * 0.5
        constrained = int(pending.sum())
        iterations = 0
        limit = (
            min(16, self.max_iterations)
            if self.active_transforms is not None
            else self.max_iterations
        )
        for step in range(limit):
            if not bool(pending.any()):
                break
            for component in range(self.matrix.shape[1]):
                gradient = s[:, component] @ residual
                updated = maximum(x[component] - gradient / self.diagonal[component], 0)

                # Already accepted pixels receive zero change. For the
                # others, update both the coefficient and its residual contribution.
                delta = where(pending, updated - x[component], 0)
                x[component] += delta
                residual += s[:, component : component + 1] * delta[None, :]
            residual, norm = check()
            pending = norm > cutoff * 0.5
            iterations = step + 1
            if not finite(norm):
                raise SolverError("NNLS produced a nonfinite residual; select 64-bit calculation.")

        # Try cached component subsets for any remaining difficult pixels.
        # Test the gradient after clipping each candidate, on this same
        # processor and precision, before replacing the current coefficients.
        if self.active_transforms is not None and bool(pending.any()):
            for transform in self.active_transforms:
                if iterations >= self.max_iterations or not bool(pending.any()):
                    break
                indices = (
                    np.flatnonzero(pending) if t is None else t.nonzero(pending, as_tuple=True)[0]
                )
                remaining = y[:, indices]
                candidate = maximum(transform @ remaining, 0)
                gradient = s.T @ (s @ candidate - remaining)
                candidate_norm = reduce(
                    absolute(where((candidate > 0) | (gradient < 0), gradient, 0))
                )
                accepted = candidate_norm <= cutoff[indices] * 0.5
                if bool(accepted.any()):
                    solved = indices[accepted]
                    x[:, solved] = candidate[:, accepted]
                    norm[solved] = candidate_norm[accepted]
                    pending = norm > cutoff * 0.5
                iterations += 1
        if not finite(x):
            raise SolverError("NNLS produced nonfinite component intensities.")

        # The internal half-threshold is a working target. Report convergence
        # against the original requested threshold, without increasing it.
        converged = bool((norm <= cutoff).all())
        max_norm = float(norm.max()) if values.shape[1] else 0.0
        result = x if t is None else x.cpu().numpy()
        return result.astype(np.float64), constrained, iterations, converged, max_norm

    def solve(self, signals):
        """

        Fits a regional measurement array in bounded blocks and combines their numerical results.

        Serialize use of the prepared solver, fit each block on the selected processor,
        and restore original pixel order in the output. Pixel counts are added across
        blocks, while the largest iteration count and gradient are retained.

        Args:
            signals: Finite normalized array of shape (bands, pixels).

        Returns:
            A SolverResult whose convergence flag is True only when every block passes
            the requested stopping test.

        Raises:
            SolverError: Input dimensions, block size or numerical values are invalid.
        """

        values = _validated_signals(signals, self.matrix.shape[0])
        if type(self.working_pixels_per_block) is not int or self.working_pixels_per_block < 1:
            raise SolverError("NNLS working block must be positive.")
        output = np.empty((self.matrix.shape[1], values.shape[1]), dtype=np.float64)
        total = iterations = 0
        converged = True
        maximum = 0.0
        inference = self._torch.inference_mode() if self._torch is not None else nullcontext()
        autocast = (
            self._torch.autocast(self.execution_device, enabled=False)
            if self._torch is not None
            else nullcontext()
        )
        with self._lock, inference, autocast:
            for offset in range(0, values.shape[1], self.working_pixels_per_block):
                block = values[:, offset : offset + self.working_pixels_per_block]
                fitted, count, steps, success, norm = self._block(block)
                output[:, offset : offset + block.shape[1]] = fitted
                total += count
                iterations = max(iterations, steps)
                converged &= success
                maximum = max(maximum, norm)
        return SolverResult(
            output,
            SolverDiagnostics(
                values.shape[1], values.shape[1] - total, total, iterations, converged, maximum
            ),
        )
