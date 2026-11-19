"""

Groups pixels that propose the same positive components so they can share a small NNLS calculation.

A proposed subset identifies which component coefficients are allowed to be
positive. Once that subset is fixed, its candidate coefficients follow from
the corresponding rows and columns of the reference Gram matrix. Grouping pixels
by the subset allows one cached inverse to operate on many pixel columns at once.

Each candidate is checked after clipping numerical negatives. Accepted columns
are returned with a Boolean solved flag; unresolved columns continue through
the calling solver's later updates.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
import numpy as np

# ==============================================================================
#                                  Grouped nnls
# ==============================================================================


def solve_grouped_candidates(gram, rhs, initial, factors, tolerance, max_rounds=8):
    """

    Fits and checks groups of pixels sharing one proposed positive-component subset.

    Represent each pixel's initial positive positions as a bit mask. Mask 5, for
    example, is binary 101 and selects components 0 and 2. Sort unresolved columns
    by this mask and apply the matching cached inverse to each group:

        coefficients[J] = inverse(gram[J, J]) @ rhs[J]

    For every candidate, require nonnegative coefficients within tolerance and a
    projected gradient within tolerance * (1 + max(abs(rhs))). The gradient is checked
    on the clipped candidate that would actually be returned.

    For unresolved pixels, retain positive candidate components and admit zero
    components with a sufficiently negative gradient. Those revised masks define
    the next grouped round. The fixed max_rounds bound prevents repeated subset
    proposals from running indefinitely.

    Args:
        gram: Prepared float64 S.T @ S matrix of shape (components, components).
        rhs: S.T @ measurements for the current pixels, shape (components, pixels).
        initial: Initial coefficient matrix whose positive positions propose the first subsets.
        factors: Cached active/inactive positions and subset inverse matrices.
        tolerance: Numerical tolerance for candidate signs and scaled gradient violations.
        max_rounds: Maximum grouped-proposal rounds, initially eight.

    Returns:
        Coefficient candidates, Boolean solved-column flags and the completed round count.
    """

    count = rhs.shape[1]
    n = gram.shape[0]
    solution = np.zeros_like(rhs)
    resolved = np.zeros(count, dtype=bool)

    # One bit represents each component, allowing a whole proposed subset
    # to be grouped by a single integer without comparing coefficient vectors.
    bits = 1 << np.arange(n, dtype=np.uint64)
    masks = np.sum((initial > 0) * bits[:, None], axis=0, dtype=np.uint64)
    lookup = {
        int(np.sum(bits[active])): (active, inactive, inverse)
        for active, inactive, inverse in factors
    }
    rhs_scale = np.max(np.abs(rhs), axis=0)
    rounds = 0
    for rounds in range(1, max_rounds + 1):
        pending = np.flatnonzero(~resolved)
        if not pending.size:
            break

        # Place identical masks together and mark where the mask changes.
        # Every resulting slice reuses one inverse from the prepared cache.
        ordered = pending[np.argsort(masks[pending], kind="stable")]
        cuts = np.r_[0, np.flatnonzero(np.diff(masks[ordered])) + 1, ordered.size]
        for start, stop in zip(cuts[:-1], cuts[1:]):
            ids = ordered[start:stop]
            mask = int(masks[ids[0]])
            x = np.zeros((n, len(ids)), dtype=np.float64)
            if mask:
                active, inactive, inverse = lookup[mask]
                x[active] = inverse @ rhs[np.ix_(active, ids)]
            gradient = gram @ x - rhs[:, ids]
            xscale = np.max(np.abs(x), axis=0)
            nonnegative = np.all(x >= -tolerance * (1 + xscale), axis=0)
            clipped = np.maximum(x, 0)

            # Evaluate the gradient after clipping, because clipping changes the proposed
            # reconstruction.
            true_gradient = gram @ clipped - rhs[:, ids]
            projected = np.where((clipped > 0) | (true_gradient < 0), true_gradient, 0)
            kkt = np.max(np.abs(projected), axis=0) <= tolerance * (1 + rhs_scale[ids])
            ok = nonnegative & kkt
            accepted = ids[ok]
            solution[:, accepted] = clipped[:, ok]
            resolved[accepted] = True
            other = ids[~ok]
            if other.size:

                # Retain positive components and admit a zero component
                # only when increasing it could reduce the reconstruction error.
                candidate = (x[:, ~ok] > 0) | (
                    (x[:, ~ok] == 0) & (gradient[:, ~ok] < -tolerance * (1 + rhs_scale[other]))
                )
                masks[other] = np.sum(candidate * bits[:, None], axis=0, dtype=np.uint64)
        if np.all(resolved):
            break
    return solution, resolved, rounds
