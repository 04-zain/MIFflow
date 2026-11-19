"""Deferred validation for the optional PySide6 desktop binding."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import importlib
import re

_VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?$"
)
_MINIMUM_VERSION = (6, 8, 0)
_NEXT_MAJOR_VERSION = (7, 0, 0)


# ==============================================================================
#                                    Binding
# ==============================================================================


@dataclass(frozen=True, slots=True)
class QtBindingInfo:
    """The validated versions used by the active Qt desktop binding."""

    binding: str
    binding_version: str
    qt_version: str


def _parse_supported_version(version: str, *, label: str) -> tuple[int, ...]:
    match = _VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise RuntimeError(
            f"{label} version must be a stable three- or four-component release, "
            f"got {version!r}"
        )
    return tuple(int(part) for part in match.groups() if part is not None)


def validate_binding_versions(pyside_version: str, qt_version: str) -> QtBindingInfo:
    """Return binding metadata when both installed Qt versions are supported."""
    pyside = _parse_supported_version(pyside_version, label="PySide6")
    qt = _parse_supported_version(qt_version, label="Qt")
    if not (_MINIMUM_VERSION <= pyside < _NEXT_MAJOR_VERSION):
        raise RuntimeError(
            "PySide6 version must be in the supported range >=6.8,<7, " f"got {pyside_version!r}"
        )
    if not (_MINIMUM_VERSION <= qt < _NEXT_MAJOR_VERSION):
        raise RuntimeError(
            "Qt version must be in the supported range >=6.8,<7, " f"got {qt_version!r}"
        )
    return QtBindingInfo(
        binding="PySide6",
        binding_version=pyside_version,
        qt_version=qt_version,
    )


def require_supported_binding() -> QtBindingInfo:
    """Load PySide6 lazily and return its validated desktop binding metadata."""
    try:
        pyside = importlib.import_module("PySide6")
        qt_core = importlib.import_module("PySide6.QtCore")
    except ImportError as error:
        raise RuntimeError(
            "Qt desktop support requires the optional 'desktop' dependency group"
        ) from error
    return validate_binding_versions(
        str(pyside.__version__),
        str(qt_core.qVersion()),
    )
