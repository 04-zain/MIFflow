"""Provide initial numerical screening limits for spectral-reference review.

The values are read from the packaged authoring presets and returned as a detached mapping.
Reference-quality validators still enforce their numeric ranges, and the accepted review
records the exact limits used for that basis."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

# ==============================================================================
#                               Reference defaults
# ==============================================================================


def fixed_basis_review_defaults() -> dict[str, float]:
    from mif_pipeline._defaults import default_section

    return default_section("unmixing.reference_quality")


def consensus_matrix_review_defaults() -> dict[str, float]:
    """Initial matrix checks for newly authored unit-L2 consensus spectra.

    The normalization tolerance is the existing reference-authoring tolerance;
    accepted historical policies continue to carry their own recorded values.
    """
    return {**fixed_basis_review_defaults(), "normalization_tolerance": 1e-6}
