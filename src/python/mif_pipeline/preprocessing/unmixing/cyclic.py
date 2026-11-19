"""Fit the independent cycle blocks of one explicitly reviewed spectral matrix.

The accepted whole-stack matrix keeps the existing reference-review and provenance
contract. Its zero entries outside each declared cycle are structural: measurements
from another staining round cannot identify this round's component abundance.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np

from mif_pipeline.io.metadata.cycles import split_cycle_component
from .solvers import SolverDiagnostics, SolverResult


@dataclass(frozen=True, slots=True)
class CycleSolveGroup:
    imaging_cycle: int
    band_indices: tuple[int, ...]
    component_indices: tuple[int, ...]


def cycle_solve_groups(channel_cycles, component_names, matrix):
    """Validate explicit cycle identities and exact independence of matrix blocks."""
    if channel_cycles is None:
        if any(split_cycle_component(name)[1] is not None for name in component_names):
            raise ValueError("Cycle-specific components need a recorded imaging cycle for every acquired band")
        return ()
    if any(type(cycle) is not int or cycle < 0 for cycle in channel_cycles):
        raise ValueError("Record an imaging cycle for every acquired band before cyclic unmixing")
    cycles = tuple(dict.fromkeys(channel_cycles))
    declared = tuple(split_cycle_component(name)[1] for name in component_names)
    # Preserve the established single-acquisition behavior for a single recorded round.
    if len(cycles) == 1 and all(cycle is None for cycle in declared):
        declared = (cycles[0],) * len(component_names)
    elif any(cycle is None for cycle in declared):
        raise ValueError(
            "Assign every output component to its imaging cycle in Channels and markers "
            "(for example, Cy5 [cycle 1]); supply a matching reference for each cycle."
        )
    if set(declared) - set(cycles):
        raise ValueError("An output component declares a cycle absent from the acquired bands")
    matrix = np.asarray(matrix)
    if matrix.shape != (len(channel_cycles), len(component_names)):
        raise ValueError("The cycle reference matrix does not cover the acquired bands and components")
    groups = []
    for cycle in cycles:
        bands = tuple(i for i, value in enumerate(channel_cycles) if value == cycle)
        components = tuple(i for i, value in enumerate(declared) if value == cycle)
        if not components:
            raise ValueError(f"Supply output components and a matching reference for cycle {cycle + 1}")
        others = tuple(i for i, value in enumerate(channel_cycles) if value != cycle)
        if others and np.any(matrix[np.ix_(others, components)] != 0):
            raise ValueError(
                f"The reference for cycle {cycle + 1} has values outside its cycle. "
                "Use matched spectra on that cycle's acquired bands and zero in other cycle rows."
            )
        block = matrix[np.ix_(bands, components)]
        if (not np.all(np.isfinite(block)) or np.any(block < 0)
                or len(components) > len(bands) or np.linalg.matrix_rank(block) != len(components)):
            raise ValueError(f"Cycle {cycle + 1} needs enough independent measured bands and a full-rank reference")
        groups.append(CycleSolveGroup(cycle, bands, components))
    return tuple(groups)


class CycleSolver:
    """Reuse the selected prepared solver separately on each cycle of a region."""

    def __init__(self, matrix, groups, constructor):
        self.groups = groups
        self.component_count = matrix.shape[1]
        self.solvers = []
        try:
            for group in groups:
                self.solvers.append(constructor(matrix[np.ix_(group.band_indices, group.component_indices)]))
        except BaseException:
            self.close()
            raise
        self.working_pixels_per_block = max(
            (getattr(solver, "working_pixels_per_block", 0) for solver in self.solvers), default=0
        ) or None

    @property
    def execution_device(self):
        return ",".join(dict.fromkeys(getattr(solver, "execution_device", "cpu") for solver in self.solvers))

    @property
    def execution_precision_policy(self):
        return ",".join(dict.fromkeys(getattr(solver, "execution_precision_policy", "float64") for solver in self.solvers))

    @property
    def verified_pixels(self):
        return sum(getattr(solver, "verified_pixels", 0) for solver in self.solvers)

    @property
    def refined_pixels(self):
        return sum(getattr(solver, "refined_pixels", 0) for solver in self.solvers)

    def solve(self, normalized_signals):
        coefficients = np.empty((self.component_count, normalized_signals.shape[1]), dtype=np.float64)
        diagnostics = []
        for group, solver in zip(self.groups, self.solvers, strict=True):
            result = solver.solve(normalized_signals[list(group.band_indices)])
            coefficients[list(group.component_indices)] = result.coefficients
            diagnostics.append(result.diagnostics)
        return SolverResult(coefficients, SolverDiagnostics(
            total_pixels=sum(item.total_pixels for item in diagnostics),
            ls_fast_path_pixels=sum(item.ls_fast_path_pixels for item in diagnostics),
            constrained_pixels=sum(item.constrained_pixels for item in diagnostics),
            iterations=max(item.iterations for item in diagnostics),
            converged=all(item.converged for item in diagnostics),
            max_projected_gradient=max(item.max_projected_gradient for item in diagnostics),
        ))

    def close(self):
        errors = []
        for solver in self.solvers:
            close = getattr(solver, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as error:
                    errors.append(error)
        if errors:
            raise errors[0]
