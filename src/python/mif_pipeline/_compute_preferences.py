"""

Checks the selected processing device and arithmetic precision before numerical work.

New computing settings select a processor. Unmixing requests own calculation
precision; older computing records retain their precision for already-pending
requests. These readers preserve that earlier choice without authoring new
global precision settings.

"""

from __future__ import annotations

import os

DEVICES = frozenset({"auto", "cpu", "cuda", "mps"})
PRECISIONS = frozenset({"float32", "float64"})


def validate_compute_device(device: str) -> str:
    """Return a supported processor choice; reject an unknown saved or entered value."""
    if not isinstance(device, str) or device not in DEVICES:
        raise ValueError("Choose automatic, CPU, NVIDIA GPU or Apple Metal GPU.")
    return device


def validate_compute_preferences(device: str, precision: str) -> tuple[str, str]:
    """

    Checks the requested device and precision against the available implementation choices.

    The automatic device option defers processor selection to the numerical
    method. CPU and CUDA requests can specify either supported precision.
    Existing precision selections remain readable. The unmixing solver uses
    CPU when its required precision is unavailable on the preferred device.

    Args:
        device: "auto", "cpu", "cuda" or "mps".
        precision: "float32" or "float64".

    Returns:
        Validated device and precision in that order.

    Raises:
        ValueError: A saved choice is unknown.
    """

    validate_compute_device(device)
    if not isinstance(precision, str) or precision not in PRECISIONS:
        raise ValueError("Choose 32-bit or 64-bit calculations.")
    return device, precision


def preferred_precision() -> str | None:
    """

    Reads the calculation precision supplied to the current processing environment.

    Returns:
        "float32" or "float64" when selected, otherwise None so the calling
        method can resolve its own configured preference.

    Raises:
        ValueError: The environment contains an unsupported precision value.
    """

    value = os.environ.get("MIF_COMPUTE_PRECISION")
    if value is not None and value not in PRECISIONS:
        raise ValueError("The saved calculation precision is invalid. Choose 32-bit or 64-bit.")
    return value


def saved_compute_preferences(settings: dict) -> tuple[str, str]:
    """

    Restores the current device and precision fields from the saved computing settings.

    We use automatic device selection when no device was supplied. An omitted
    precision resolves to float32 for an explicitly selected Metal device and
    float64 otherwise. The complete combination is checked before it is returned
    to the application or processing worker.

    Args:
        settings: Saved dictionary containing optional "device" and "precision" fields.

    Returns:
        Checked device and precision tuple used to initialize processing choices.

    Raises:
        ValueError: Settings are not a dictionary or contain unsupported choices.
    """

    if not isinstance(settings, dict):
        raise ValueError("Saved computing settings must be a settings object.")
    device = settings.get("device", "auto")
    precision = settings.get("precision")
    if precision is None:
        precision = "float32" if device == "mps" else "float64"
    return validate_compute_preferences(device, precision)
