"""Portable typed checksums for spectral-reference exchange evidence."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import hashlib
import json
import math
import struct

# ==============================================================================
#                                 Typed checksum
# ==============================================================================


class TypedChecksumError(ValueError):
    """A value cannot be represented by the closed checksum type system."""


def typed_checksum_value(value: object) -> object:
    """Return the closed tagged form used for cross-language evidence hashes."""
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypedChecksumError("non-finite number is not permitted")
        return ["f64", struct.pack(">d", value).hex()]
    if isinstance(value, list):
        return ["list", [typed_checksum_value(item) for item in value]]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypedChecksumError("map keys must be strings")
        return [
            "map",
            [[key, typed_checksum_value(value[key])] for key in sorted(value)],
        ]
    raise TypedChecksumError(f"unsupported checksum value type {type(value).__name__}")


def typed_checksum_sha256(value: object) -> str:
    """Hash a typed form whose JSON contains no untagged numeric values."""
    encoded = json.dumps(
        typed_checksum_value(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["TypedChecksumError", "typed_checksum_sha256", "typed_checksum_value"]
