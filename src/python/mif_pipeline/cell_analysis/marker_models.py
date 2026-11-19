"""Constrained two-component fits and their monotone intensity score curves.

We fit on the supplied finite observations, preserve repeated-value weights, and
correct fitted positive-component responsibilities only after fitting finishes,
using the same empirical positive-component increasing score correction for both families.
The scores describe fitted component membership. Biological probability calibration
requires separate reference labels. The corrected score and selected cutoff determine
the decision; numerical fit failures remain explicit.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping, Sequence

import numpy as np
from scipy.optimize import LinearConstraint, brentq, minimize
from scipy.special import digamma, gammaln, logsumexp


# ==============================================================================
#                        Monotone scores and decision boundary
# ==============================================================================

@dataclass(frozen=True, slots=True)
class MonotoneScore:
    """Retains every distinct fitting intensity and its corrected score.

    The correction determines scores at observed intensities. Linear interpolation
    extends that empirical curve between its knots; endpoint clipping extends it
    outside the fitted support. These extensions are explicit modelling assumptions.
    Missing/nonfinite values remain unknown. A declared zero score restricts the
    domain to nonnegative values and handles exact zero separately from the knots.
    """

    x: np.ndarray
    scores: np.ndarray
    zero_score: float | None = None

    def predict(self, values: Sequence[float] | np.ndarray) -> np.ndarray:
        """Evaluate the persisted curve, leaving nonfinite measurements as NaN."""
        values = np.asarray(values, dtype=np.float64)
        result = np.full(values.shape, np.nan)
        valid = np.isfinite(values)
        if self.zero_score is not None:
            valid &= values > 0
            result[values == 0] = self.zero_score
        preceding = np.searchsorted(self.x, values[valid], side='right') - 1
        fitted = self.scores[np.clip(preceding, 0, len(self.x) - 1)].copy()
        interior = (preceding >= 0) & (preceding < len(self.x) - 1)
        left = preceding[interior]
        low, high = self.x[left], self.x[left + 1]
        query = values[valid][interior]
        # Divide intensity differences before multiplying score differences. This
        # avoids an overflowing slope for distinct subnormal intensity knots.
        with np.errstate(over='ignore'):
            span, distance = high - low, query - low
        extreme = ~np.isfinite(span)
        span[extreme] = .5 * high[extreme] - .5 * low[extreme]
        distance[extreme] = .5 * query[extreme] - .5 * low[extreme]
        fraction = distance / span
        fitted[interior] = np.minimum(self.scores[left + 1], self.scores[left]
            + fraction * (self.scores[left + 1] - self.scores[left]))
        result[valid] = fitted
        return result

    def decision(self, cutoff: float) -> tuple[str, float | None]:
        """Find the first float64 intensity with score >= cutoff, or a constant.

        A plateau equal to the cutoff is positive. A curve with no crossing has
        an explicit constant outcome. We solve the crossing within its adjacent
        saved intensity pair and check the score at that candidate and the
        preceding float64 value. If either check fails, at most 64 evaluations
        locate the first representable intensity satisfying the saved score rule.
        This calculation occurs once per curve and cutoff. If every positive
        value reaches the cutoff but the explicit zero score does not, the gate
        is x > 0, represented exactly for float64 as x >= nextafter(0, +inf).
        """
        if not np.isfinite(cutoff) or not 0 < cutoff < 1:
            raise ValueError('score cutoff must lie strictly between zero and one')
        if self.scores[0] >= cutoff:
            if self.zero_score is not None and self.zero_score < cutoff:
                return 'threshold', float(np.nextafter(0., np.inf))
            return 'all_positive', None
        if self.scores[-1] < cutoff:
            return 'all_negative', None
        upper = int(np.searchsorted(self.scores, cutoff, side='left'))
        fraction = ((cutoff - self.scores[upper - 1]) /
                    (self.scores[upper] - self.scores[upper - 1]))
        threshold = float((1 - fraction) * self.x[upper - 1] + fraction * self.x[upper])
        pair = self.predict([np.nextafter(threshold, -np.inf), threshold])
        if pair[0] < cutoff <= pair[1]:
            return 'threshold', threshold
        # Rounding may make the first positive float differ from the algebraic
        # crossing. Search ordered float64 values using the actual evaluator so
        # score >= cutoff and intensity >= threshold also agree at exact ties.
        # Ordered keys span fewer than 2**64 positions, so at most 64 scalar
        # bisections suffice, independent of the number of application cells.
        # This work occurs once per fitted curve/cutoff, never per cell.
        sign, mask = 1 << 63, (1 << 64) - 1

        def ordered(value):
            bits = int(np.float64(value).view(np.uint64))
            return (~bits & mask) if bits & sign else bits | sign

        def value(key):
            bits = (key & (sign - 1)) if key & sign else (~key & mask)
            return np.uint64(bits).view(np.float64).item()

        lower_key, upper_key = ordered(self.x[upper - 1]), ordered(self.x[upper])
        for _ in range(64):
            if upper_key - lower_key <= 1:
                break
            middle = (lower_key + upper_key) // 2
            if self.predict([value(middle)])[0] >= cutoff:
                upper_key = middle
            else:
                lower_key = middle
        return 'threshold', value(upper_key)


def increasing_positive_score(values, scores) -> MonotoneScore:
    """Apply positive-component increasing score correction on sorted, distinct fitting intensities.

    An already increasing score is unchanged. Otherwise start at its minimum and
    retain only positive successive increments. The operational lower-component
    score is one minus this result. Mixture fitting also calculates the independent
    lower-component correction for normalization diagnostics.

    Two Gamma components have log odds C + A*log(x) - B*x; two Gaussian
    components have quadratic log odds. Their positive responsibility has at
    most one interior extremum. Its total positive
    variation plus its minimum therefore stays in [0,1] in exact arithmetic.
    We reject substantive range violations. Input slack is eight float64 eps;
    cumulative output slack is eight eps per support point, an allowance for
    arithmetic roundoff only. Values inside that slack are clipped to [0,1].
    """
    x, raw = np.asarray(values, dtype=float), np.asarray(scores, dtype=float)
    if (x.ndim != 1 or not len(x) or x.shape != raw.shape
            or not np.all(np.isfinite(x)) or not np.all(np.isfinite(raw))
            or np.any(x[1:] <= x[:-1])):
        raise ValueError('increasing score correction requires aligned finite scores and sorted distinct intensities')
    epsilon = 8 * np.finfo(float).eps
    if np.any(raw < -epsilon) or np.any(raw > 1 + epsilon):
        raise NumericalFitError('increasing_input_score_out_of_range')
    bounded = np.clip(raw, 0., 1.)
    increments = np.r_[np.min(bounded), np.diff(bounded)]
    corrected = (np.cumsum(np.maximum(increments, 0.))
                 if np.any(increments < 0) else bounded.copy())
    tolerance = epsilon * len(x)
    if np.any(corrected < -tolerance) or np.any(corrected > 1 + tolerance):
        raise NumericalFitError('increasing_corrected_score_out_of_range')
    return MonotoneScore(x, np.clip(corrected, 0., 1.))


def _increasing_component_scores(values, responsibilities) -> np.ndarray:
    """Calculate both independently corrected increasing component score columns.

    Columns retain lower/higher-component order. The original lower posterior is
    corrected in reverse intensity order; the original higher posterior uses
    increasing order. Both use the same roundoff safeguards as ``increasing_positive_score``.
    For a two-Gamma or two-Gaussian posterior these columns are bounded, but on a
    finite observation window their sum can differ from one. Their sum residual
    is diagnostic. Decisions use the higher column and its exact complement.
    """
    x, raw = np.asarray(values, dtype=float), np.asarray(responsibilities, dtype=float)
    if x.ndim != 1 or raw.shape != (len(x), 2):
        raise ValueError('increasing score correction component scores require aligned values and two posterior columns')
    positive = increasing_positive_score(x, raw[:, 1]).scores
    negative = increasing_positive_score(-x[::-1], raw[::-1, 0]).scores[::-1]
    return np.column_stack((negative, positive))


def _increasing_fit_score(values, responsibilities, *, zero_score=None) -> tuple[MonotoneScore, dict]:
    """Build either mixture family's operational curve and matching diagnostics.

    Both original posterior columns are corrected independently for the
    normalization diagnostic. The selected curve always uses the positive
    column; its operational negative is the complement. Gamma supplies its
    separate exact-zero score, while Gaussian retains its full real domain.
    """
    independent = _increasing_component_scores(values, responsibilities)
    curve = MonotoneScore(np.asarray(values, dtype=float), independent[:, 1], zero_score)
    raw_positive = np.asarray(responsibilities, dtype=float)[:, 1]
    metadata = {
        'monotone_method': 'increasing_positive_score',
        'monotone_score_convention': 'positive_score_negative_complement',
        'independent_component_sum_max_absolute_residual':
            float(np.max(np.abs(independent.sum(axis=1) - 1.))),
        'monotone_roundoff_tolerance': 8 * np.finfo(float).eps * len(curve.x),
        'monotone_max_absolute_adjustment': float(np.max(np.abs(curve.scores - raw_positive))),
    }
    return curve, metadata


def resolve_location_bounds(configuration, values, *, gamma=False) -> np.ndarray:
    """Resolve numeric or {'quantile': q} component bounds on the complete scope.

    Quantiles use NumPy's linear interpolation and q in [0,1]. None denotes an
    unbounded endpoint. Gamma mode bounds are independent intervals attached to
    the lower/higher-mean components; their modes need not be ordered. Gaussian
    mean bounds must also permit the required lower/higher ordering.
    """
    bounds = np.array([[0 if gamma else -np.inf, np.inf],
                       [0 if gamma else -np.inf, np.inf]], dtype=float)
    if configuration is None:
        return bounds
    if not isinstance(configuration, (list, tuple)) or len(configuration) != 2:
        raise ValueError('component location bounds must contain two [lower, upper] pairs')
    for component, pair in enumerate(configuration):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError('each component requires a lower and upper bound')
        for side, endpoint in enumerate(pair):
            if endpoint is None:
                continue
            if isinstance(endpoint, Mapping):
                if set(endpoint) != {'quantile'}:
                    raise ValueError('a percentile endpoint must contain only quantile')
                q = endpoint['quantile']
                if isinstance(q, bool) or not isinstance(q, (int, float)) or not 0 <= q <= 1:
                    raise ValueError('quantile must be a number between zero and one')
                if len(values) == 0:
                    raise ValueError('percentile bounds require finite scope observations')
                value = float(np.quantile(values, q, method='linear'))
            else:
                if isinstance(endpoint, bool) or not isinstance(endpoint, (int, float)):
                    raise ValueError('a location bound must be numeric, null or a quantile')
                value = float(endpoint)
            if not np.isfinite(value) or (gamma and value < 0):
                raise ValueError('location bounds must be finite and Gamma modes nonnegative')
            bounds[component, side] = value
    if np.any(bounds[:, 0] > bounds[:, 1]):
        raise ValueError('component location bounds contain an empty interval')
    if not gamma and bounds[0, 0] > bounds[1, 1]:
        raise ValueError('Gaussian mean bounds have no ordered feasible solution')
    return bounds


# ==============================================================================
#                             Finite mixture calculation
# ==============================================================================

class NumericalFitError(ValueError):
    """Identifies a numerical failure without interpreting it as marker negativity."""


@dataclass(frozen=True, slots=True)
class MixtureFit:
    """Returns fitted parameters, sampled responsibilities and density evaluation."""
    curve: MonotoneScore
    parameters: dict
    densities: np.ndarray
    iterations: int
    converged: bool


def _posterior(log_components, counts):
    """Evaluate a mixture in log space and retain its weighted log likelihood."""
    normalizer = logsumexp(log_components, axis=1)
    if not np.all(np.isfinite(normalizer)):
        raise NumericalFitError('nonfinite_likelihood')
    return np.exp(log_components - normalizer[:, None]), float(counts @ normalizer)


def _ordered_means(means, weights, bounds):
    """Solve the two-variable bounded ordered weighted least-squares update."""
    result = np.clip(means, bounds[:, 0], bounds[:, 1])
    if result[0] > result[1]:
        lower, upper = max(bounds[:, 0]), min(bounds[:, 1])
        if lower > upper:
            raise NumericalFitError('infeasible_component_order')
        result[:] = np.clip(np.dot(means, weights) / np.sum(weights), lower, upper)
    return result


def _normal_log(x, means, variances, proportions):
    """Return both weighted Gaussian log densities in one vectorized operation."""
    return (np.log(proportions)[None, :] - .5 * np.log(2 * np.pi * variances)[None, :]
            - .5 * (x[:, None] - means[None, :]) ** 2 / variances[None, :])


def fit_gaussian(values, density_x, bounds, *, variance_mode='shared', starts=6,
                 max_iterations=100, tolerance=1e-6, variance_floor_relative=1e-8, seed=0):
    """Fit a constrained two-Gaussian mixture and apply the increasing score correction.

    One median initialization and seeded random quantile starts are reproducible.
    Shared variance uses the total responsibility-weighted residual sum of squares
    around the constrained means. Unshared variance uses an ECM mean update with
    weights N_k / variance_k followed by each component's residual variance.
    Every update increases the same constrained likelihood. Bounds are applied
    during fitting; the numerical variance floor stabilizes evaluation and is
    recorded in measurement units. The highest-likelihood converged start supplies
    the result. Failure of every start is retained as a numerical failure.
    """
    x, counts = np.unique(values, return_counts=True)
    if len(x) < 2:
        raise NumericalFitError('constant_intensity')
    center = float(np.median(values))
    scale = float(np.max(np.abs(x - center)))
    if not np.isfinite(scale) or scale <= 0:
        raise NumericalFitError('intensity_scale_not_representable')
    z, limits = (x - center) / scale, (bounds - center) / scale
    total = float(np.sum(counts))
    floor = float(variance_floor_relative)
    best, failures = None, []
    # One median split anchors the fit; seeded random split quantiles explore
    # different feasible starting partitions reproducibly across repeated runs.
    initial_splits = np.r_[.5, np.random.default_rng(seed).uniform(.05, .95, starts - 1)]
    for split in initial_splits:
        partition = z < float(np.quantile((np.asarray(values) - center) / scale, split))
        if not np.any(partition) or np.all(partition):
            partition = z <= z[max(0, min(len(z) - 2, int(split * len(z))))]
        responsibility = np.column_stack((partition, ~partition)).astype(float)
        masses = counts @ responsibility
        means = _ordered_means((counts[:, None] * responsibility * z[:, None]).sum(0) / masses,
                               masses, limits)
        variances = np.full(2, max(float(np.average((z - np.average(z, weights=counts)) ** 2,
                                                   weights=counts)), floor))
        proportions = masses / total
        history, converged = [], False
        try:
            responsibility, old_ll = _posterior(_normal_log(z, means, variances, proportions), counts)
            history.append(old_ll - total * np.log(scale))
            for iteration in range(1, max_iterations + 1):
                weighted = counts[:, None] * responsibility
                masses = weighted.sum(0)
                if np.any(masses <= np.finfo(float).tiny):
                    raise NumericalFitError('component_mass_numerically_zero')
                unconstrained = (weighted * z[:, None]).sum(0) / masses
                new_means = _ordered_means(unconstrained, masses / variances, limits)
                residual = (weighted * (z[:, None] - new_means) ** 2).sum(0)
                if variance_mode == 'shared':
                    new_variances = np.full(2, max(float(residual.sum() / total), floor))
                else:
                    new_variances = np.maximum(residual / masses, floor)
                new_proportions = masses / total
                new_r, ll = _posterior(_normal_log(z, new_means, new_variances, new_proportions), counts)
                if ll < old_ll - 1e-9 * (1 + abs(old_ll)):
                    raise NumericalFitError('likelihood_decreased')
                means, variances, proportions = new_means, new_variances, new_proportions
                responsibility = new_r
                history.append(ll - total * np.log(scale))
                if abs(ll - old_ll) <= tolerance * (1 + abs(old_ll)):
                    converged = True
                    break
                old_ll = ll
            if not converged:
                raise NumericalFitError('maximum_iterations_reached')
            if means[1] <= means[0]:
                raise NumericalFitError('component_locations_coincide')
            if best is None or history[-1] > best[0]:
                best = (history[-1], means.copy(), variances.copy(), proportions.copy(),
                        responsibility.copy(), iteration, history)
        except (NumericalFitError, FloatingPointError, ValueError) as error:
            failures.append(str(error))
    if best is None:
        raise NumericalFitError(failures[0] if failures else 'no_valid_initialization')
    _, means, variances, proportions, raw, iteration, history = best
    curve, score_metadata = _increasing_fit_score(x, raw)
    densities = np.exp(_normal_log((density_x - center) / scale, means, variances, proportions)).T / scale
    params = {'means': tuple((means * scale + center).tolist()),
              'variances': tuple((variances * scale ** 2).tolist()),
              'weights': tuple(proportions.tolist()), 'variance_mode': variance_mode,
              'variance_floor': floor * scale ** 2, 'starts': starts,
              'failed_starts': tuple(failures), 'initialization_seed': seed,
              'initial_split_quantiles': tuple(initial_splits.tolist()),
              'initialization': 'median_and_seeded_random_quantiles',
              'log_likelihood_history': tuple(history),
              'log_likelihood': history[-1], 'fit_population_count': int(total),
              **score_metadata}
    return MixtureFit(curve, params, densities, iteration, True)


def _gamma_log(x, shapes, rates, proportions):
    """Return weighted Gamma log densities for strictly positive observations."""
    return (np.log(proportions)[None, :] + shapes * np.log(rates) - gammaln(shapes)
            + (shapes - 1) * np.log(x[:, None]) - x[:, None] * rates)


def _gamma_modes(parameters):
    return np.maximum(parameters[::2] - 1, 0) / parameters[1::2]


def _gamma_component(mean, mean_log):
    """Solve the weighted Gamma shape score equation and obtain rate=shape/mean."""
    difference = np.log(mean) - mean_log
    if difference <= 1e-12:
        raise NumericalFitError('gamma_component_variance_collapsed')
    shape = brentq(lambda a: np.log(a) - digamma(a) - difference, 1e-8, 1e12)
    return shape, shape / mean


def _gamma_feasible(parameters, bounds, tolerance=1e-8):
    modes = _gamma_modes(parameters)
    return (np.all(np.isfinite(parameters)) and np.all(parameters > 0)
            and np.all(modes >= bounds[:, 0] - tolerance)
            and np.all(modes <= bounds[:, 1] + tolerance)
            and parameters[0] / parameters[1] <= parameters[2] / parameters[3] + tolerance)


def _project_gamma_initial(parameters, bounds):
    """Make a bounded initializer feasible for mean ordering in log parameters.

    Mode intervals are independent. Their initial clipping may invert component
    means, so project only those infeasible starts before evaluating likelihood.
    The linear log-parameter constraint is log(a1/b1) >= log(a0/b0).
    """
    if _gamma_feasible(parameters, bounds):
        return parameters
    target = np.log(parameters)
    constraints = [LinearConstraint(np.array([[-1., 1., 1., -1.]]), [0.], [np.inf])]
    for k, (lower, upper) in enumerate(bounds):
        if lower > 0:
            constraints.append({'type': 'ineq', 'fun': lambda p, k=k, l=lower:
                np.exp(p[2*k]) - 1 - np.exp(p[2*k+1]) * l})
        if np.isfinite(upper):
            constraints.append({'type': 'ineq', 'fun': lambda p, k=k, u=upper:
                1 + np.exp(p[2*k+1]) * u - np.exp(p[2*k])})
    result = minimize(lambda p: (.5 * np.sum((p-target)**2), p-target), target,
        jac=True, method='SLSQP', constraints=constraints,
        bounds=[(np.log(1e-8), np.log(1e12)), (np.log(1e-12), np.log(1e16))]*2,
        options={'maxiter':200, 'ftol':1e-10})
    projected = np.exp(result.x)
    if not result.success or not _gamma_feasible(projected, bounds, 1e-7):
        raise NumericalFitError('gamma_mean_initialization_not_feasible')
    return projected


def _gamma_update(weighted, z, previous, bounds):
    """Maximize weighted Gamma log likelihood with mode bounds and mean ordering.

    Unconstrained weighted shape estimates solve the digamma score equation. When
    they violate a location bound, the four-parameter constrained objective uses
    sufficient statistics and an analytic gradient. For a positive lower mode L,
    shape-1-rate*L >= 0; an upper U gives 1+rate*U-shape >= 0. A zero lower bound
    deliberately permits shape < 1, whose actual Gamma mode is zero.
    """
    masses = weighted.sum(0)
    if np.any(masses <= np.finfo(float).tiny):
        raise NumericalFitError('component_mass_numerically_zero')
    means = (weighted * z[:, None]).sum(0) / masses
    logs = (weighted * np.log(z[:, None])).sum(0) / masses
    estimate = np.array([v for mean, log in zip(means, logs, strict=True)
                         for v in _gamma_component(float(mean), float(log))])
    if _gamma_feasible(estimate, bounds):
        return estimate
    fractions = masses / masses.sum()

    def objective(p):
        a, b = p[::2], p[1::2]
        terms = a * np.log(b) - gammaln(a) + (a - 1) * logs - b * means
        gradient = np.empty(4)
        gradient[::2] = -fractions * (np.log(b) - digamma(a) + logs)
        gradient[1::2] = -fractions * (a / b - means)
        return -float(fractions @ terms), gradient

    constraints = []
    for k, (lower, upper) in enumerate(bounds):
        if lower > 0:
            constraints.append({'type': 'ineq', 'fun': lambda p, k=k, l=lower:
                                p[2*k] - 1 - p[2*k+1] * l})
        if np.isfinite(upper):
            constraints.append({'type': 'ineq', 'fun': lambda p, k=k, u=upper:
                                1 + p[2*k+1] * u - p[2*k]})
    constraints.append({'type': 'ineq', 'fun': lambda p: p[2] / p[3] - p[0] / p[1]})
    result = minimize(objective, previous, jac=True, method='SLSQP',
                      bounds=[(1e-8, 1e12), (1e-12, 1e16)] * 2,
                      constraints=constraints, options={'maxiter': 200, 'ftol': 1e-10})
    if not result.success or not _gamma_feasible(result.x, bounds, 1e-7):
        raise NumericalFitError('gamma_constrained_optimization_failed')
    if result.fun > objective(previous)[0] + 1e-8:
        raise NumericalFitError('gamma_m_step_decreased')
    return result.x


def fit_gamma(values, density_x, bounds, *, starts=6, max_iterations=100, tolerance=1e-6, seed=0):
    """Fit two conditional-positive Gamma components ordered by mean.

    Optional mode bounds apply independently to the lower/higher-mean components.
    Their modes may be reversed or both zero; the higher-mean component can have
    greater relative variation. The positive-component increasing score correction
    supplies the increasing score after fitting. The supplied scope must contain
    nonnegative intensities. Exact zeros receive score zero separately from the
    Gamma likelihood; this is an explicit model assumption. Numerical scaling
    uses the median positive intensity and is inverted in returned parameters.
    The likelihood preserves observed occurrence counts. The correction uses
    original responsibilities at unique observed positive intensities. Both
    corrected columns are calculated for a sum-residual diagnostic; the
    positive score and its operational negative complement determine decisions.
    The exact-zero rule is stored separately from the positive support knots.
    """
    if np.any(np.asarray(values) < 0):
        raise NumericalFitError('gamma_requires_nonnegative_intensities')
    positive = np.asarray(values)[np.asarray(values) > 0]
    x, counts = np.unique(positive, return_counts=True)
    if len(x) < 2:
        raise NumericalFitError('insufficient_positive_intensity_variation')
    scale = float(np.median(positive))
    z, limits = x / scale, bounds / scale
    if not np.all(np.isfinite(z)) or np.any(z <= 0):
        raise NumericalFitError('intensity_scale_not_representable')
    total = float(counts.sum())
    best, failures = None, []
    # One median split anchors the fit; seeded random split quantiles explore
    # different feasible starting partitions reproducibly across repeated runs.
    initial_splits = np.r_[.5, np.random.default_rng(seed).uniform(.05, .95, starts - 1)]
    for split in initial_splits:
        partition = z < np.quantile(positive / scale, split)
        if not np.any(partition) or np.all(partition):
            partition = z <= z[max(0, min(len(z) - 2, int(split * len(z))))]
        r = np.column_stack((partition, ~partition)).astype(float)
        weighted = counts[:, None] * r
        masses = weighted.sum(0)
        means = (weighted * z[:, None]).sum(0) / masses
        variances = (weighted * (z[:, None] - means) ** 2).sum(0) / masses
        shapes = np.maximum(means ** 2 / np.maximum(variances, 1e-8), .1)
        rates = shapes / means
        p = np.column_stack((shapes, rates)).ravel()
        target_modes = np.clip(_gamma_modes(p), limits[:, 0], limits[:, 1])
        for k, mode in enumerate(target_modes):
            if mode == 0:
                p[2*k] = min(p[2*k], 1.)
            else:
                p[2*k] = max(p[2*k], 1.1)
                p[2*k+1] = (p[2*k] - 1) / mode
        proportions = masses / total
        history, converged = [], False
        try:
            p = _project_gamma_initial(p, limits)
            r, old_ll = _posterior(_gamma_log(z, p[::2], p[1::2], proportions), counts)
            history.append(old_ll - total * np.log(scale))
            for iteration in range(1, max_iterations + 1):
                weighted = counts[:, None] * r
                new_p = _gamma_update(weighted, z, p, limits)
                new_proportions = weighted.sum(0) / total
                new_r, ll = _posterior(_gamma_log(z, new_p[::2], new_p[1::2], new_proportions), counts)
                if ll < old_ll - 1e-8 * (1 + abs(old_ll)):
                    raise NumericalFitError('likelihood_decreased')
                p, proportions, r = new_p, new_proportions, new_r
                history.append(ll - total * np.log(scale))
                if abs(ll - old_ll) <= tolerance * (1 + abs(old_ll)):
                    converged = True
                    break
                old_ll = ll
            if not converged:
                raise NumericalFitError('maximum_iterations_reached')
            if p[2] / p[3] <= p[0] / p[1]:
                raise NumericalFitError('component_means_coincide')
            if best is None or history[-1] > best[0]:
                best = (history[-1], p.copy(), proportions.copy(), r.copy(), iteration, history)
        except (NumericalFitError, FloatingPointError, ValueError) as error:
            failures.append(str(error))
    if best is None:
        raise NumericalFitError(failures[0] if failures else 'no_valid_initialization')
    _, p, proportions, raw, iteration, history = best
    curve, score_metadata = _increasing_fit_score(x, raw, zero_score=0.)
    zeros = int(np.count_nonzero(np.asarray(values) == 0))
    densities = np.zeros((2, len(density_x)))
    positive_grid = density_x > 0
    densities[:, positive_grid] = np.exp(_gamma_log(density_x[positive_grid] / scale,
        p[::2], p[1::2], proportions)).T / scale
    params = {'shapes': tuple(p[::2].tolist()), 'rates': tuple((p[1::2] / scale).tolist()),
              'scales': tuple((scale / p[1::2]).tolist()),
              'modes': tuple((_gamma_modes(p) * scale).tolist()),
              'means': tuple((p[::2] / p[1::2] * scale).tolist()),
              'component_ordering': 'mean',
              'optimizer': 'expectation_constrained_maximization_mean_order',
              'weights': tuple(proportions.tolist()), 'starts': starts,
              'failed_starts': tuple(failures), 'initialization_seed': seed,
              'initial_split_quantiles': tuple(initial_splits.tolist()),
              'initialization': 'median_and_seeded_random_quantiles',
              'log_likelihood_history': tuple(history),
              'log_likelihood': history[-1], 'fit_population_count': len(values),
              'positive_fit_count': int(total), 'zero_count': zeros,
              'conditional_positive_fit': True,
              'zero_policy': 'conditional_positive_zero_score_0',
              'zero_score': 0.,
              **score_metadata}
    return MixtureFit(curve, params, densities, iteration, True)
