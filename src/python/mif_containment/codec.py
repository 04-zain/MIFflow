"""Bounded canonical JSON encoding for containment protocol packets.

The implementation exchanges explicit process, scope and operation records through the
declared host interfaces. Native lifecycle observations are checked before the corresponding
process-control result is accepted."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

MAXIMUM_COLLECTION_LENGTH = 4_096
MAXIMUM_NODE_COUNT = 16_384


# ==============================================================================
#                                     Codec
# ==============================================================================


def loads_canonical_packet(
    raw: bytes,
    *,
    maximum_bytes: int = 65_536,
    maximum_depth: int = 12,
) -> object:
    """Decode one bounded, byte-exact canonical UTF-8 JSON value."""

    if type(raw) is not bytes:
        raise TypeError("raw packet must be bytes")
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise ValueError("maximum_bytes must be a positive integer")
    if type(maximum_depth) is not int or maximum_depth <= 0:
        raise ValueError("maximum_depth must be a positive integer")
    if len(raw) > maximum_bytes:
        raise ValueError("packet exceeds maximum byte length")

    try:
        decoded_text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("packet is not valid UTF-8") from exc

    _check_lexical_depth(decoded_text, maximum_depth=maximum_depth)
    decoded = json.loads(
        decoded_text,
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_nonfinite,
    )
    _check_decoded_bounds(decoded)

    if _encode_json(decoded) != raw:
        raise ValueError("packet is not canonical JSON")
    return _freeze(decoded)


def dumps_canonical(value: object) -> bytes:
    """Encode a JSON-compatible value as canonical UTF-8 bytes."""

    return _encode_json(_plain_json_value(value))


def sha256_hex(raw: bytes) -> str:
    """Return the lowercase SHA-256 digest of exact bytes."""

    if type(raw) is not bytes:
        raise TypeError("raw digest material must be bytes")
    return hashlib.sha256(raw).hexdigest()


def _check_lexical_depth(text: str, *, maximum_depth: int) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > maximum_depth:
                raise ValueError("packet exceeds maximum nesting depth")
        elif character in "]}":
            depth -= 1


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, value in pairs:
        if key in decoded:
            raise ValueError(f"duplicate key: {key!r}")
        decoded[key] = value
    return decoded


def _reject_nonfinite(value: str) -> object:
    raise ValueError(f"non-finite number is forbidden: {value}")


def _check_decoded_bounds(root: object) -> None:
    pending = [root]
    node_count = 0
    while pending:
        node = pending.pop()
        node_count += 1
        if node_count > MAXIMUM_NODE_COUNT:
            raise ValueError("packet exceeds maximum node count")
        if isinstance(node, dict):
            if len(node) > MAXIMUM_COLLECTION_LENGTH:
                raise ValueError("object exceeds maximum collection length")
            pending.extend(node.values())
        elif isinstance(node, list):
            if len(node) > MAXIMUM_COLLECTION_LENGTH:
                raise ValueError("array exceeds maximum collection length")
            pending.extend(node)


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _plain_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json_value(item) for item in value]
    return value


def _encode_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
