"""

Estimates fluorescence-component coefficients from normalized pixel spectra.

We represent reference spectra as matrix S, with measured bands in rows
and components in columns. The measurement matrix Y has bands in rows and
pixels in columns. Coefficients A scale the reference spectra so S @ A
reconstructs the measured spectrum of each pixel.

Clipped least squares calculates A = pinv(S) @ Y and sets negative
coefficients to zero. Nonnegative least squares minimizes the reconstruction
error while keeping all coefficients at least zero. This second method can
change the remaining coefficients when one reaches its zero boundary.

We prepare the small reference-matrix calculations once and reuse them
across pixel blocks. Pixels with straightforward solutions finish at the
initial least-squares check; only the remaining pixels require the grouped
component fits and coordinate updates described below.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
from numpy.typing import NDArray
from mif_pipeline._defaults import default_section

_SOLVER_DEFAULTS = default_section("unmixing")


# ==============================================================================
#                                    Solvers
# ==============================================================================


class SolverError(ValueError):
    """

    Reports invalid reference spectra, measurement arrays or numerical fitting settings.
    """

@dataclass(frozen=True, slots=True)
class SolverDiagnostics:
    """

    Records which numerical calculations were needed and whether fitting met the selected tolerance.

    The pixel counts distinguish the initial least-squares result from pixels
    requiring nonnegative updates. The convergence flag states whether every
    NNLS pixel passed its final stopping check; we inspect that flag before
    accepting a processed image.

    Attributes:
        total_pixels: Number of measured pixel columns supplied to the solve.
        ls_fast_path_pixels: Columns accepted directly from the initial LS calculation.
        constrained_pixels: NNLS columns requiring additional fitting.
        iterations: Grouped rounds plus coordinate sweeps. Multiple internal
            blocks report the largest count; final subset trials are not counted.
        converged: Whether all NNLS columns satisfy their final check. Clipped
            LS records successful completion of its fixed calculation.
        max_projected_gradient: Largest remaining NNLS gradient violation,
            before the per-pixel tolerance scaling. Clipped LS records 0.0.
    """

    total_pixels: int
    ls_fast_path_pixels: int
    constrained_pixels: int
    iterations: int
    converged: bool
    max_projected_gradient: float


@dataclass(frozen=True, slots=True)
class SolverResult:
    """

    Retains fitted component amounts together with their stopping information.

    Coefficients remain in the scale of the reference columns. We apply the
    component reporting weights separately before storing spectral-total intensities.

    Attributes:
        coefficients: Float64 array of shape (components, pixels).
        diagnostics: Pixel counts, iteration information and convergence result.
    """

    coefficients: NDArray[np.float64]
    diagnostics: SolverDiagnostics


class ClippedLeastSquaresSolver:
    """

    Fits pixel spectra using one prepared least-squares transform and clips negative coefficients.

    We calculate the reference pseudoinverse when the solver is constructed.
    Each later solve uses one matrix multiplication for all supplied pixels,
    followed by setting coefficients below zero to zero.

    Attributes:
        transform: Prepared float64 pseudoinverse with shape (components, bands).
    """

    def __init__(self, spectral_matrix: NDArray[np.generic]) -> None:
        """

        Prepares the reference pseudoinverse for repeated pixel-spectrum fitting.

        NumPy calculates the singular-value decomposition S = U @ Sigma @ V.T.
        Replacing retained singular values by their reciprocals gives
        pinv(S) = V @ Sigma_plus @ U.T. Applying this matrix to a measured spectrum
        produces its unconstrained least-squares coefficients. We retain the
        transform so this decomposition is performed once for the selected reference.

        Args:
            spectral_matrix: Finite reference matrix of shape (bands, components).
                Components must be linearly independent, with at least as many
                bands as components.

        Raises:
            SolverError: Matrix dimensions, values or numerical rank are invalid.
        """

        self._spectral = _validated_spectral_matrix(spectral_matrix)
        transform = np.linalg.pinv(self._spectral)
        transform.setflags(write=False)
        self.transform = transform

    def solve(self, normalized_signals: NDArray[np.generic]) -> SolverResult:
        """

        Calculates least-squares coefficients for a pixel block and sets negative values to zero.

        We minimize the sum of squared reconstruction errors without a sign
        restriction by calculating pinv(S) @ Y. Clipping then gives:

            coefficients = maximum(pinv(S) @ Y, 0)

        The positive coefficients keep their original LS values. Because overlapping
        reference spectra contribute to the same measured bands, removing a negative
        coefficient can leave those positive values above their NNLS optimum.
        The two estimators therefore need separate descriptions and result labels.

        Args:
            normalized_signals: Finite array of shape (bands, pixels) in reference-band order.

        Returns:
            SolverResult with float64 component coefficients and completion diagnostics.

        Raises:
            SolverError: Measurement dimensions, band count or values are invalid.
        """

        signals = _validated_signals(normalized_signals, self._spectral.shape[0])
        coefficients = self.transform @ signals
        np.maximum(coefficients, 0.0, out=coefficients)
        count = signals.shape[1]
        return SolverResult(
            coefficients=coefficients,
            diagnostics=SolverDiagnostics(
                total_pixels=count,
                ls_fast_path_pixels=count,
                constrained_pixels=0,
                iterations=0,
                converged=True,
                max_projected_gradient=0.0,
            ),
        )


class BatchedNnlsSolver:
    """

    Fits nonnegative component amounts for many pixels sharing one reference matrix.

    We first check the unconstrained LS result. Pixels already satisfying the
    nonnegative stopping conditions need no further fitting. For the others,
    pixels proposing the same positive components share small matrix solves,
    followed where necessary by component-by-component updates.

    Attributes:
        spectral_matrix: Float64 reference matrix with bands in rows.
        tolerance: Selected stopping tolerance, scaled for each pixel.
        max_iterations: Limit for grouped rounds and coordinate sweeps.
        working_pixels_per_block: Maximum columns per internal solve, initially 65536.
        transform: Prepared pseudoinverse used for the initial LS result.
        gram: Shared S.T @ S matrix describing products between component spectra.
    """

    def __init__(
        self,
        spectral_matrix: NDArray[np.generic],
        *,
        tolerance: float = _SOLVER_DEFAULTS["nnls_tolerance"],
        max_iterations: int = _SOLVER_DEFAULTS["nnls_max_iterations"],
    ) -> None:
        """

        Prepares the reference calculations reused during nonnegative pixel fitting.

        We calculate pinv(S) for the initial coefficients and G = S.T @ S for
        later updates. For at most twelve components, we also prepare the inverse
        of G[J,J] for each nonempty component subset J. A subset identifies the
        components temporarily allowed to be positive while the others remain zero.

        There are 2**C - 1 such subsets for C components: 255 for eight and 4095
        for twelve. We restrict this preparation to small panels because its size
        grows exponentially. Larger panels retain the coordinate-update method.

        Args:
            spectral_matrix: Finite band-by-component matrix with independent columns.
            tolerance: Positive finite stopping tolerance from the selected settings.
            max_iterations: Positive limit for grouped rounds and coordinate sweeps.

        Raises:
            SolverError: The reference matrix or fitting limits are invalid.
        """

        if not math.isfinite(tolerance) or tolerance <= 0:
            raise SolverError("NNLS tolerance must be finite and positive")
        if max_iterations <= 0:
            raise SolverError("NNLS maximum iterations must be positive")
        self.spectral_matrix = _validated_spectral_matrix(spectral_matrix)
        self.tolerance = float(tolerance)
        self.working_pixels_per_block = 65536
        self.max_iterations = int(max_iterations)
        transform = np.linalg.pinv(self.spectral_matrix)
        gram = self.spectral_matrix.T @ self.spectral_matrix
        transform.setflags(write=False)
        gram.setflags(write=False)
        self.transform = transform
        self.gram = gram
        self._active_set_factors = _active_set_factors(gram)

    def solve(self, normalized_signals: NDArray[np.generic]) -> SolverResult:
        """

        Minimizes spectral reconstruction error while keeping every fitted component coefficient nonnegative.

        We minimize L(A) = 0.5 * sum((S @ A - Y)**2) independently for each pixel.
        With G = S.T @ S and H = S.T @ Y, the gradient is G @ A - H. A negative
        gradient means increasing a coefficient can reduce the error; a positive
        gradient means decreasing it can reduce the error.

        At a positive coefficient, the optimum requires a zero gradient. At a
        coefficient already equal to zero, a positive gradient is allowed because
        the coefficient cannot decrease below zero. We therefore check the gradient
        only where coefficients are positive or the gradient is negative. This
        produces the projected-gradient array Q used in the stopping condition:

            max(abs(Q[:, p])) <= tolerance * (1 + max(abs(H[:, p])))

        We calculate the result through the following steps:

        i) Split inputs above 65536 pixels into internal blocks, preserving their
        original output-column positions. Calculate initial LS coefficients for
        each block, clip negative values and accept columns whose original signs
        and clipped gradients satisfy the selected tolerances.

        ii) Group unresolved pixels by their proposed positive components. For
        a subset J, calculate A[J] = inverse(G[J,J]) @ H[J], with omitted rows
        fixed at zero. Pixels sharing J reuse the same prepared inverse. This
        route is enabled for small panels and repeats at most eight grouped rounds.

        iii) Update one component at a time across all unresolved pixel columns:

            A[j] = maximum(0, (H[j] - G[j] @ A + G[j,j]*A[j]) / G[j,j])

        We hold the other components at their current values while minimizing this
        one-coordinate quadratic. After a complete pass through the components,
        remove columns that satisfy the stopping condition. With prepared subsets,
        coordinate work is capped before the final subset search.

        iv) For eligible small-panel fits, test the remaining cached subsets for
        unresolved pixels. Recheck Q for the assembled result, including columns
        accepted earlier. Any unresolved or failing column sets converged=False.
        The image-writing workflow checks that result before accepting output.

        Args:
            normalized_signals: Finite band-by-pixel measurements in reference-row
                order. Offset subtraction can produce negative measurements; the
                nonnegative restriction applies to the fitted component coefficients.

        Returns:
            SolverResult with float64 component-by-pixel coefficients and the
            initial-fit, constrained-fit, iteration and convergence information.

        Raises:
            SolverError: Measurement shape, band count or numerical values are invalid.
        """

        signals = _validated_signals(normalized_signals, self.spectral_matrix.shape[0])
        if signals.shape[1] > self.working_pixels_per_block:
            coefficients = np.empty(
                (self.spectral_matrix.shape[1], signals.shape[1]), dtype=np.float64
            )
            diagnostics = []
            for start in range(0, signals.shape[1], self.working_pixels_per_block):
                block = self.solve(signals[:, start : start + self.working_pixels_per_block])
                coefficients[:, start : start + self.working_pixels_per_block] = block.coefficients
                diagnostics.append(block.diagnostics)
            return SolverResult(
                coefficients,
                SolverDiagnostics(
                    total_pixels=signals.shape[1],
                    ls_fast_path_pixels=sum(d.ls_fast_path_pixels for d in diagnostics),
                    constrained_pixels=sum(d.constrained_pixels for d in diagnostics),
                    iterations=max(d.iterations for d in diagnostics),
                    converged=all(d.converged for d in diagnostics),
                    max_projected_gradient=max(d.max_projected_gradient for d in diagnostics),
                ),
            )

        # Step 1: accept only initial fits that satisfy both the coefficient
        # signs and the constrained-gradient check after numerical clipping.
        unconstrained = self.transform @ signals
        coefficients = np.maximum(0.0, unconstrained)
        rhs_all = self.spectral_matrix.T @ signals
        cutoff = self.tolerance * (1.0 + np.max(np.abs(rhs_all), axis=0))
        initial_gradient = _projected_gradient(self.gram @ coefficients - rhs_all, coefficients)
        fast_mask = np.all(unconstrained >= -self.tolerance, axis=0) & (
            np.max(np.abs(initial_gradient), axis=0) <= cutoff
        )
        constrained_indices = np.flatnonzero(~fast_mask)
        iterations = 0
        converged = True
        remaining = constrained_indices

        # Step 2: group pixels with the same positive components so they share a cached inverse.
        if (
            self._active_set_factors is not None
            and remaining.size
            and self.max_iterations >= 2 * self.gram.shape[0]
        ):
            from .grouped_nnls import solve_grouped_candidates

            candidate, accepted, rounds = solve_grouped_candidates(
                self.gram,
                rhs_all[:, remaining],
                coefficients[:, remaining],
                self._active_set_factors,
                self.tolerance,
                max_rounds=min(8, self.max_iterations),
            )
            coefficients[:, remaining[accepted]] = candidate[:, accepted]
            remaining = remaining[~accepted]
            iterations = rounds

        # Step 3: update one component across unresolved pixels and remove columns as they
        # converge.
        if remaining.size:
            remaining_indices = remaining.copy()
            rhs = rhs_all[:, remaining_indices]
            x = coefficients[:, remaining_indices].copy()
            converged = False
            completed_rounds = iterations
            iteration_limit = max(0, self.max_iterations - completed_rounds)
            if self._active_set_factors is not None:
                iteration_limit = min(
                    iteration_limit,
                    max(32, 4 * self.gram.shape[0]),
                )
            for iteration in range(1, iteration_limit + 1):
                for component in range(self.gram.shape[0]):
                    diagonal = self.gram[component, component]

                    # Remove the other components' current contributions,
                    # solve this coordinate, and enforce its zero lower bound.
                    partial = rhs[component] - self.gram[component] @ x + diagonal * x[component]
                    x[component] = np.maximum(0.0, partial / diagonal)
                gradient = self.gram @ x - rhs
                projected = _projected_gradient(gradient, x)
                projected_norm = np.max(np.abs(projected), axis=0)
                rhs_scale = np.max(np.abs(rhs), axis=0)
                iterations = completed_rounds + iteration
                if np.all(projected_norm <= self.tolerance * (1.0 + rhs_scale)):
                    coefficients[:, remaining_indices] = x
                    converged = True
                    break
                converged_columns = projected_norm <= self.tolerance * (1.0 + rhs_scale)
                if np.any(converged_columns):
                    coefficients[:, remaining_indices[converged_columns]] = x[:, converged_columns]
                    keep = ~converged_columns
                    remaining_indices = remaining_indices[keep]
                    rhs = rhs[:, keep]
                    x = x[:, keep]

            # Step 4: for small libraries, try the cached subsets for any
            # difficult pixels left after the bounded coordinate sweeps.
            if not converged and remaining_indices.size:
                if (
                    self._active_set_factors is not None
                    and self.max_iterations >= 2 * self.gram.shape[0]
                ):
                    exact, resolved = _solve_active_set_remainder(
                        self.gram,
                        rhs,
                        self._active_set_factors,
                        self.tolerance,
                    )
                    if np.any(resolved):
                        coefficients[:, remaining_indices[resolved]] = exact[:, resolved]
                    keep = ~resolved
                    remaining_indices = remaining_indices[keep]
                    x = x[:, keep]
                if remaining_indices.size:
                    coefficients[:, remaining_indices] = x
                else:
                    converged = True

        # Check the final coefficients against the full projected-gradient stopping rule.
        projected = _projected_gradient(
            self.gram @ coefficients - rhs_all,
            coefficients,
        )
        max_projected = float(np.max(np.abs(projected), initial=0.0))
        converged = bool(converged and np.all(np.max(np.abs(projected), axis=0) <= cutoff))
        return SolverResult(
            coefficients=coefficients,
            diagnostics=SolverDiagnostics(
                total_pixels=signals.shape[1],
                ls_fast_path_pixels=int(np.count_nonzero(fast_mask)),
                constrained_pixels=int(constrained_indices.size),
                iterations=iterations,
                converged=converged,
                max_projected_gradient=max_projected,
            ),
        )


def apply_spectral_column_sums(
    spectral_matrix: NDArray[np.generic],
    solver_coefficients: NDArray[np.generic],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """

    Converts fitted coefficients to total modeled intensity across the measured bands.

    Component j contributes coefficient[j,p] * S[:,j] to pixel p. Summing that
    contribution over bands gives:

        weight[j] = sum(S[:,j])
        intensity[j,p] = weight[j] * coefficient[j,p]

    We apply this conversion once after fitting, using the same reference matrix
    as the solve. A unit-sum reference column has weight one; other column
    normalizations require their actual sums.

    Args:
        spectral_matrix: Exact band-by-component matrix used to obtain the coefficients.
        solver_coefficients: Finite component-by-pixel coefficients in reference-column order.

    Returns:
        Float64 component-intensity matrix and its per-component reporting weights.

    Raises:
        SolverError: Reference or coefficient dimensions/values are invalid.
    """

    spectral = _validated_spectral_matrix(spectral_matrix)
    coefficients = np.asarray(solver_coefficients, dtype=np.float64)
    if coefficients.ndim != 2 or coefficients.shape[0] != spectral.shape[1]:
        raise SolverError("solver coefficient shape must be (spectral components, pixels)")
    if not np.all(np.isfinite(coefficients)):
        raise SolverError("solver coefficients must be finite")

    # Summing down rows gives one weight per component column. The added
    # singleton axis applies that weight to every pixel for that component.
    column_sums = np.sum(spectral, axis=0, dtype=np.float64)
    reported_intensities = column_sums[:, None] * coefficients
    return reported_intensities, column_sums


def _validated_spectral_matrix(
    value: NDArray[np.generic],
) -> NDArray[np.float64]:
    """

    Checks that the reference matrix contains enough independent measurements to fit its components.

    We require a finite two-dimensional matrix with at least as many bands as
    components and independent component columns. If one column is a linear
    combination of others, the same measured spectrum could have multiple
    equivalent coefficient solutions. Such a reference fails the rank check.

    Args:
        value: Proposed band-by-component reference matrix.

    Returns:
        Independent read-only float64 copy used by solver preparation.

    Raises:
        SolverError: The matrix is empty, nonfinite or lacks full numerical column rank.
    """

    matrix = np.asarray(value, dtype=np.float64)
    if matrix.ndim != 2 or not matrix.size:
        raise SolverError("spectral matrix must be a non-empty two-dimensional array")
    if not np.all(np.isfinite(matrix)):
        raise SolverError("spectral matrix values must be finite")
    if matrix.shape[0] < matrix.shape[1] or np.linalg.matrix_rank(matrix) != matrix.shape[1]:
        raise SolverError("spectral matrix must have full column rank")
    result = np.array(matrix, dtype=np.float64, order="C", copy=True)
    result.setflags(write=False)
    return result


def _validated_signals(
    value: NDArray[np.generic],
    band_count: int,
) -> NDArray[np.float64]:
    """

    Checks the measurement-band axis and prepares float64 values for the selected solve.

    Each column must contain one pixel's complete measured spectrum in the
    reference's band order. We allow finite negative offset-corrected values
    and reject NaN or infinity before matrix multiplication.

    Args:
        value: Measurement array of shape (bands, pixels).
        band_count: Number of bands required by the prepared reference.

    Returns:
        Finite float64 measurement array.

    Raises:
        SolverError: Dimensions, band count or numerical values are invalid.
    """

    signals = np.asarray(value, dtype=np.float64)
    if signals.ndim != 2 or signals.shape[0] != band_count:
        raise SolverError(f"normalized signal shape must be ({band_count}, pixels)")
    if not np.all(np.isfinite(signals)):
        raise SolverError("normalized signals must be finite")
    return signals


def _projected_gradient(
    gradient: NDArray[np.float64],
    coefficients: NDArray[np.float64],
) -> NDArray[np.float64]:
    """

    Retains gradient values indicating a feasible improvement to the nonnegative fit.

    We retain either gradient sign at a positive coefficient, since that
    coefficient can increase or decrease. At zero, only a negative gradient
    indicates improvement through an allowed increase. A nonnegative gradient
    at zero contributes zero to the stopping check.

    Args:
        gradient: G @ coefficients - S.T @ measurements, with components in rows.
        coefficients: Current nonnegative coefficients with the same array shape.

    Returns:
        Remaining gradient violations used to check NNLS convergence.
    """

    return np.where((coefficients > 0.0) | (gradient < 0.0), gradient, 0.0)


def _active_set_factors(
    gram: NDArray[np.float64],
) -> tuple[tuple[NDArray[np.int64], NDArray[np.int64], NDArray[np.float64]], ...] | None:
    """

    Prepares small inverse matrices for every nonempty subset of components when C is at most twelve.

    We represent a subset by an integer whose bit j indicates whether component
    j is included. For example, binary 101 includes components 0 and 2. We
    select those rows and columns from G, calculate the inverse and retain the
    included and omitted indices together. The saved subsets are ordered from
    largest to smallest for the later difficult-pixel search.

    Args:
        gram: Float64 G = S.T @ S for the checked reference spectra.

    Returns:
        Tuples of included indices, omitted indices and inverse matrix for
        the 2**C - 1 nonempty subsets. Returns None above twelve components.
    """

    component_count = gram.shape[0]
    if component_count > 12:
        return None
    factors = []

    # There are 2**component_count subsets; omit the empty subset because
    # its all-zero candidate is tested directly by the remainder solver.
    for mask in range(1, 1 << component_count):
        active = np.asarray(
            [index for index in range(component_count) if mask & (1 << index)],
            dtype=np.int64,
        )
        inactive = np.asarray(
            [index for index in range(component_count) if not mask & (1 << index)],
            dtype=np.int64,
        )
        inverse = np.linalg.inv(gram[np.ix_(active, active)])
        inverse.setflags(write=False)
        factors.append((active, inactive, inverse))
    factors.sort(key=lambda item: item[0].size, reverse=True)
    return tuple(factors)


def _solve_active_set_remainder(
    gram: NDArray[np.float64],
    rhs: NDArray[np.float64],
    factors: tuple[tuple[NDArray[np.int64], NDArray[np.int64], NDArray[np.float64]], ...],
    tolerance: float,
) -> tuple[NDArray[np.float64], NDArray[np.bool_]]:
    """

    Tests prepared component subsets for pixels still unresolved after the earlier NNLS updates.

    We first test the all-zero solution. For each remaining subset J, calculate
    A[J] = inverse(G[J,J]) @ H[J] across unresolved pixels. The included
    coefficients must be nonnegative within tolerance, and omitted components
    must not have a negative gradient large enough to require an increase.

    Once a column passes, we retain its coefficients and omit it from later
    subset trials. The main solve recalculates the complete stopping condition
    on all assembled coefficients before reporting convergence.

    Args:
        gram: Shared component-product matrix S.T @ S.
        rhs: H = S.T @ Y for unresolved pixel columns.
        factors: Prepared subset indices and inverse matrices.
        tolerance: Selected sign and per-pixel gradient tolerance.

    Returns:
        Candidate coefficients and a boolean vector identifying resolved
        columns. Unresolved columns remain marked False for the main solve.
    """

    result = np.zeros_like(rhs)
    resolved = np.zeros(rhs.shape[1], dtype=bool)
    rhs_scale = np.max(np.abs(rhs), axis=0)
    zero_solution = np.all(rhs <= tolerance * (1.0 + rhs_scale), axis=0)
    resolved |= zero_solution
    for active, inactive, inverse in factors:
        pending = np.flatnonzero(~resolved)
        if not pending.size:
            break
        values = inverse @ rhs[np.ix_(active, pending)]
        coefficient_scale = np.max(np.abs(values), axis=0)
        feasible = np.all(values >= -tolerance * (1.0 + coefficient_scale), axis=0)

        # An omitted component is zero; a negative gradient would indicate improvement by
        # increasing it.
        if inactive.size:
            gradient = gram[np.ix_(inactive, active)] @ values - rhs[np.ix_(inactive, pending)]
            feasible &= np.all(gradient >= -tolerance * (1.0 + rhs_scale[pending]), axis=0)
        accepted = pending[feasible]
        if accepted.size:
            result[np.ix_(active, accepted)] = np.maximum(0.0, values[:, feasible])
            resolved[accepted] = True
    return result, resolved
