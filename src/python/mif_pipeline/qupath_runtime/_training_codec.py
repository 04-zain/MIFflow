"""Shared bounded canonical codec for native-training exchange records.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any

from mif_pipeline.pipeline_core.durable_records import (
    DurableRecordError,
    canonical_json_bytes,
)

# ==============================================================================
#                                 Training codec
# ==============================================================================


class QuPathNativeTrainingExchangeError(ValueError):
    """A native-training request or result is malformed or unauthentic."""


_MAX_EXCHANGE_BYTES = 16 * 1024 * 1024
_MAX_JSON_DEPTH = 32
_MAX_TEXT_BYTES = 4096
_MAX_COLLECTION = 65_536
_MAX_CLASSIFIER_BYTES = 64 * 1024 * 1024 * 1024
_JAVA_INT_MAX = 2_147_483_647
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _closed_mapping(value: object, fields: frozenset[str], label: str) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or any(type(key) is not str for key in value)
        or set(value) != fields
    ):
        raise QuPathNativeTrainingExchangeError(f"{label} fields are invalid")
    return value


def _array(value: object, label: str, *, nonempty: bool = False) -> list[object]:
    if type(value) is not list or len(value) > _MAX_COLLECTION:
        raise QuPathNativeTrainingExchangeError(f"{label} must be a bounded array")
    if nonempty and not value:
        raise QuPathNativeTrainingExchangeError(f"{label} must be nonempty")
    return value


def _text(value: object, label: str) -> str:
    try:
        size = len(value.encode("utf-8", errors="strict")) if type(value) is str else 0
    except UnicodeError as exc:
        raise QuPathNativeTrainingExchangeError(f"{label} is invalid text") from exc
    if type(value) is not str or not value.strip() or "\0" in value or size > _MAX_TEXT_BYTES:
        raise QuPathNativeTrainingExchangeError(f"{label} is invalid text")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise QuPathNativeTrainingExchangeError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _integer(value: object, label: str, *, minimum: int = 0, maximum: int = _JAVA_INT_MAX) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise QuPathNativeTrainingExchangeError(f"{label} is invalid")
    return value


def _finite(
    value: object,
    label: str,
    *,
    minimum: float | None = None,
    positive: bool = False,
) -> float:
    if type(value) not in (int, float):
        raise QuPathNativeTrainingExchangeError(f"{label} must be finite")
    try:
        result = float(value)
    except OverflowError as exc:
        raise QuPathNativeTrainingExchangeError(f"{label} must be finite") from exc
    if not math.isfinite(result):
        raise QuPathNativeTrainingExchangeError(f"{label} must be finite")
    if minimum is not None and (result < minimum or (positive and result == minimum)):
        raise QuPathNativeTrainingExchangeError(f"{label} is outside its range")
    return result


def _literal(value: object, expected: object, label: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise QuPathNativeTrainingExchangeError(f"{label} must be {expected!r}")


def _export_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {key: _export_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export_json(item) for item in value]
    return value


def _canonical_digest(value: object) -> str:
    try:
        payload = canonical_json_bytes(_export_json(value))
    except DurableRecordError as exc:
        raise QuPathNativeTrainingExchangeError("exchange value is not canonical JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise QuPathNativeTrainingExchangeError("native-training JSON contains a duplicate key")
        result[key] = value
    return result


def _reject_nonfinite(token: str) -> object:
    raise QuPathNativeTrainingExchangeError(
        f"native-training JSON contains nonfinite number {token}"
    )


def _json_shape(value: object, *, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise QuPathNativeTrainingExchangeError("native-training JSON exceeds its depth bound")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                raise QuPathNativeTrainingExchangeError("native-training JSON keys must be strings")
            _json_shape(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_shape(item, depth=depth + 1)
    elif value is None or type(value) in (str, bool, int):
        return
    elif type(value) is float and math.isfinite(value):
        return
    else:
        raise QuPathNativeTrainingExchangeError(
            "native-training JSON contains an unsupported value"
        )


def _decode_canonical(payload: bytes, label: str) -> Mapping[str, object]:
    if type(payload) is not bytes or not payload or len(payload) > _MAX_EXCHANGE_BYTES:
        raise QuPathNativeTrainingExchangeError(f"{label} exceeds its bounded byte contract")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
        _json_shape(value)
        canonical = canonical_json_bytes(value)
    except QuPathNativeTrainingExchangeError:
        raise
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
        DurableRecordError,
    ) as exc:
        raise QuPathNativeTrainingExchangeError(f"{label} is invalid JSON") from exc
    if not isinstance(value, Mapping) or canonical != payload:
        raise QuPathNativeTrainingExchangeError(f"{label} is not canonical JSON")
    return value


def _ordered_texts(value: object, label: str, *, sorted_order: bool) -> tuple[str, ...]:
    items = tuple(
        _text(item, f"{label}[{index}]")
        for index, item in enumerate(_array(value, label, nonempty=True))
    )
    if len(set(items)) != len(items) or (sorted_order and items != tuple(sorted(items))):
        raise QuPathNativeTrainingExchangeError(f"{label} must be nonempty, unique, and canonical")
    return items
