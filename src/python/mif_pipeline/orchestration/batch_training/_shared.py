"""Shared validation and canonicalization primitives for batch training.

Training records keep subject roles, reference evidence, model settings and application
scope linked. Their validators and native adapters are called by the shared training service
before a fit, evaluation or accepted publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import TypeAlias

from mif_pipeline.pipeline_core import ROIAnalysisError

from ..batch_artifacts import AnalysisDomainBinding

JsonValue: TypeAlias = (
    None | bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
)
_SHA256_LENGTH = 64
_SPLITS = ("training", "validation", "test")
_SPLIT_RANK = {name: index for index, name in enumerate(_SPLITS)}
# ==============================================================================
#                                     Shared
# ==============================================================================


class BatchTrainingError(ROIAnalysisError):
    """Invalid or non-authoritative batch training material."""


class ClassifierKind(str, Enum):
    PRIMARY_COMPARTMENT = "primary_compartment"
    SECONDARY_STRUCTURE = "secondary_structure"
    SUPERVISED_PHENOTYPE = "supervised_phenotype"


class EvaluationIntent(str, Enum):
    EXPLORATORY = "exploratory"
    INDEPENDENT_PERFORMANCE = "independent_performance"
    TRAINING_ONLY = "training_only"


class ClassifierArtifactKind(str, Enum):
    QUPATH_NATIVE = "qupath-native"
    PYTHON_MODEL = "python-model"


def _classifier_training_producer_kind(kind: ClassifierKind) -> str:
    try:
        return {
            ClassifierKind.PRIMARY_COMPARTMENT: ("primary_classifier_training_review"),
            ClassifierKind.SECONDARY_STRUCTURE: ("secondary_classifier_training_review"),
            ClassifierKind.SUPERVISED_PHENOTYPE: ("supervised_phenotype_training_review"),
        }[kind]
    except (KeyError, TypeError) as exc:
        raise BatchTrainingError("classifier kind has no training producer") from exc


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BatchTrainingError(f"{field} must be a non-empty string")
    return value


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BatchTrainingError(f"{field} must be a lowercase SHA-256")
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise BatchTrainingError(f"{field} must be an integer")
    if value < 0:
        raise BatchTrainingError(f"{field} must be nonnegative")
    return value


def _closed_mapping(
    value: object,
    *,
    fields: frozenset[str],
    field: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BatchTrainingError(f"{field} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise BatchTrainingError(f"{field} keys must be strings")
    actual = frozenset(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise BatchTrainingError(f"{field} fields are invalid: {'; '.join(details)}")
    return value


def _array(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise BatchTrainingError(f"{field} must be an array")
    return value


def _freeze_json(value: object, field: str) -> JsonValue:
    if value is None or type(value) is bool or isinstance(value, str):
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise BatchTrainingError(f"{field} must contain finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise BatchTrainingError(f"{field} keys must be non-empty strings")
            frozen[key] = _freeze_json(item, f"{field}.{key}")
        return MappingProxyType(dict(sorted(frozen.items())))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, f"{field}[{index}]") for index, item in enumerate(value))
    raise BatchTrainingError(f"{field} must contain only finite JSON values")


def _json_object(value: object, field: str) -> Mapping[str, JsonValue]:
    frozen = _freeze_json(value, field)
    if not isinstance(frozen, Mapping):
        raise BatchTrainingError(f"{field} must be an object")
    return frozen


def _export_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _export_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_export_json(item) for item in value]
    if isinstance(value, Enum):
        return _export_json(value.value)
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            _export_json(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise BatchTrainingError("training material must be finite canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _enum(value: object, enum_type: type[Enum], field: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise BatchTrainingError(f"{field} is not a supported value") from exc


def _string_tuple(
    value: object,
    field: str,
    *,
    nonempty: bool,
    canonical: bool,
) -> tuple[str, ...]:
    result = tuple(
        _text(item, f"{field}[{index}]") for index, item in enumerate(_array(value, field))
    )
    if nonempty and not result:
        raise BatchTrainingError(f"{field} must not be empty")
    if len(set(result)) != len(result):
        raise BatchTrainingError(f"{field} must contain unique values")
    if canonical and result != tuple(sorted(result)):
        raise BatchTrainingError(f"{field} must use canonical order")
    return result


def _sha256_tuple(
    value: object,
    field: str,
    *,
    nonempty: bool,
) -> tuple[str, ...]:
    result = tuple(
        _sha256(item, f"{field}[{index}]") for index, item in enumerate(_array(value, field))
    )
    if nonempty and not result:
        raise BatchTrainingError(f"{field} must not be empty")
    if result != tuple(sorted(result)) or len(set(result)) != len(result):
        raise BatchTrainingError(f"{field} must be canonical and unique")
    return result


def _binding_sort_key(binding: AnalysisDomainBinding) -> tuple[object, ...]:
    return (
        binding.sample_id,
        binding.slide_id,
        binding.analysis_domain_id,
        binding.analysis_domain_revision,
        binding.project_entry_id,
    )


def _binding_owner_key(binding: AnalysisDomainBinding) -> tuple[object, ...]:
    return _binding_sort_key(binding)


def _binding_identity(binding: AnalysisDomainBinding) -> bytes:
    return _canonical_bytes(binding.to_mapping())


def _binding_row_identity(binding: AnalysisDomainBinding) -> tuple[str, str, str, str]:
    return (
        binding.project_entry_id,
        binding.analysis_domain_id,
        binding.analysis_domain_revision_sha256,
        binding.domain_set_sha256,
    )


def _version_inventory(
    value: object,
    field: str,
    *,
    nonempty: bool = False,
    require_canonical: bool = True,
) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for index, item in enumerate(_array(value, field)):
        pair = _array(item, f"{field}[{index}]")
        if len(pair) != 2:
            raise BatchTrainingError(f"{field}[{index}] must be a name/version pair")
        result.append(
            (
                _text(pair[0], f"{field}[{index}].name"),
                _text(pair[1], f"{field}[{index}].version"),
            )
        )
    parsed = tuple(result)
    if nonempty and not parsed:
        raise BatchTrainingError(f"{field} must not be empty")
    if len({name for name, _ in parsed}) != len(parsed):
        raise BatchTrainingError(f"{field} must contain unique names")
    ordered = tuple(sorted(parsed))
    if require_canonical and parsed != ordered:
        raise BatchTrainingError(f"{field} must be canonical with unique names")
    return ordered


def _normalized_native_locator(value: object, *, require_canonical: bool) -> str:
    raw = _text(value, "native_artifact_project_relative_path")
    windows = PureWindowsPath(raw)
    if windows.drive or windows.root or raw.startswith(("/", "\\")):
        raise BatchTrainingError("native artifact locator must be project-relative")
    normalized_text = raw.replace("\\", "/")
    segments = normalized_text.split("/")
    if any(
        segment in ("", ".", "..") or any(character in '<>:"|?*' for character in segment)
        for segment in segments
    ):
        raise BatchTrainingError("native artifact locator must be portable canonical path text")
    path = PurePosixPath(normalized_text)
    if (
        not path.parts
        or path.parts[0] != "classifiers"
        or len(path.parts) < 2
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise BatchTrainingError("native artifact locator must be a safe project-relative path")
    normalized = path.as_posix()
    if any(ord(character) < 32 for character in normalized):
        raise BatchTrainingError("native artifact locator contains unsafe characters")
    if require_canonical and normalized != raw:
        raise BatchTrainingError("native artifact locator must use canonical POSIX form")
    return normalized


def _read_exact_artifact_bytes(
    locator: Path,
    expected_sha256: str | None,
    expected_byte_count: int | None,
    *,
    field: str,
) -> tuple[str, int]:
    try:
        content = locator.read_bytes()
    except OSError as exc:
        raise BatchTrainingError(f"{field} bytes are not readable") from exc
    digest = hashlib.sha256(content).hexdigest()
    count = len(content)
    if expected_sha256 is not None and digest != _sha256(expected_sha256, f"{field}_sha256"):
        raise BatchTrainingError(f"{field} bytes do not match their SHA-256")
    if expected_byte_count is not None:
        expected = _integer(expected_byte_count, f"{field}_byte_count")
        if count != expected:
            raise BatchTrainingError(f"{field} bytes do not match their byte count")
    if count == 0:
        raise BatchTrainingError(f"{field} bytes must not be empty")
    return digest, count
