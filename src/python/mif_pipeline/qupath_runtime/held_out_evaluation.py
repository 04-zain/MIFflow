"""Closed exchange facade for native held-out classifier evaluation."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from ._held_out_evaluation_request import (
    HeldOutEvaluationExchangeError,
    QuPathHeldOutEvaluationRequest,
    _issue_qupath_held_out_evaluation_request,
    decode_qupath_held_out_evaluation_request,
)
from ._held_out_evaluation_result import (
    QuPathHeldOutEvaluationResult,
    _issue_qupath_held_out_evaluation_result,
    decode_qupath_held_out_evaluation_result,
)

for _facade_symbol in (
    QuPathHeldOutEvaluationRequest,
    QuPathHeldOutEvaluationResult,
    decode_qupath_held_out_evaluation_request,
    decode_qupath_held_out_evaluation_result,
    _issue_qupath_held_out_evaluation_request,
    _issue_qupath_held_out_evaluation_result,
):
    _facade_symbol.__module__ = __name__
del _facade_symbol


__all__ = [
    "HeldOutEvaluationExchangeError",
    "QuPathHeldOutEvaluationRequest",
    "QuPathHeldOutEvaluationResult",
    "decode_qupath_held_out_evaluation_request",
    "decode_qupath_held_out_evaluation_result",
]
