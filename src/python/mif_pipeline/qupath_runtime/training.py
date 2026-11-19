"""Closed exchange records for native multi-image QuPath training.

This public facade preserves the exchange API while private modules own shared codec,
request, and result implementation details."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from ._training_codec import QuPathNativeTrainingExchangeError
from ._training_request import (
    QuPathNativeTrainingRequest,
    _issue_qupath_native_training_request,
    decode_qupath_native_training_request,
)
from ._training_result import (
    QuPathNativeTrainingResult,
    _issue_qupath_native_training_result,
    decode_qupath_native_training_result,
)

for _facade_symbol in (
    QuPathNativeTrainingExchangeError,
    QuPathNativeTrainingRequest,
    QuPathNativeTrainingResult,
    decode_qupath_native_training_request,
    decode_qupath_native_training_result,
    _issue_qupath_native_training_request,
    _issue_qupath_native_training_result,
):
    _facade_symbol.__module__ = __name__
del _facade_symbol


__all__ = [
    "QuPathNativeTrainingExchangeError",
    "QuPathNativeTrainingRequest",
    "QuPathNativeTrainingResult",
    "decode_qupath_native_training_request",
    "decode_qupath_native_training_result",
]
