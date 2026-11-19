"""Validated deterministic QuPath/Python JSON exchange transport.

The `ParsedStageResultExchange` record carries `result`, `result_payload_sha256`,
`result_byte_count`, `generation_manifest`, `generation_manifest_payload_sha256` and its
remaining declared fields.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit
from uuid import uuid4

from mif_pipeline.pipeline_core.errors import ROIAnalysisError, validate_safe_path_segment
from mif_pipeline.pipeline_core.stage_result import (
    StageResult,
    _requires_generation_manifest as requires_generation_manifest,
)

REQUEST_SCHEMA = "roi-stage-request/v2"
RESULT_SCHEMA = "roi-stage-result/v1"
GENERATION_MANIFEST_SCHEMA = "roi-generation-manifest/v1"
_MAX_EXCHANGE_BYTES = 16 * 1024 * 1024
_MAX_JSON_DEPTH = 32


# ==============================================================================
#                                    Exchange
# ==============================================================================


class _StageResultExchangeErrorCode(str, Enum):
    """Typed first-applicable failure category for producer result members."""

    RESULT_PAYLOAD_INVALID = "result_payload_invalid"
    GENERATION_MANIFEST_PAYLOAD_INVALID = "generation_manifest_payload_invalid"
    RESULT_GENERATION_BINDING_INVALID = "result_generation_binding_invalid"
    RESULT_TARGET_AUTHORITY_INVALID = "result_target_authority_invalid"


class ExchangeContractError(ROIAnalysisError):
    """Raised when a machine-readable QuPath exchange violates its schema."""


def validate_canonical_image_uri(value: object) -> str:
    """Return exact valid canonical file-URI text or raise ExchangeContractError."""
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or len(value.encode("utf-8")) > 4096
        or "?" in value
        or "#" in value
        or any(
            character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
            for character in value
        )
        or "\\" in value
    ):
        raise ExchangeContractError("canonical image URI must be bounded ASCII text")
    for index, character in enumerate(value):
        if character == "%" and (
            index + 2 >= len(value)
            or value[index + 1] not in "0123456789abcdefABCDEF"
            or value[index + 2] not in "0123456789abcdefABCDEF"
        ):
            raise ExchangeContractError("canonical image URI has an invalid percent escape")
        if character == "%" and int(value[index + 1 : index + 3], 16) in {
            ord("/"),
            ord("\\"),
        }:
            raise ExchangeContractError("canonical image URI has an encoded separator")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ExchangeContractError("canonical image URI is invalid") from exc
    if (
        not value.startswith("file:")
        or parsed.scheme != "file"
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        raise ExchangeContractError("canonical image URI must be an absolute file URI")

    authority = parsed.netloc
    path = parsed.path
    raw_location = value.removeprefix("file:")
    if authority:
        labels = authority.split(".")
        if (
            any(
                not label
                or len(label) > 63
                or not label[0].isalnum()
                or not label[-1].isalnum()
                or any(not (character.isalnum() or character == "-") for character in label)
                for label in labels
            )
            or path == "/"
        ):
            raise ExchangeContractError("canonical image URI has an invalid UNC authority")
    elif not raw_location.startswith("///") or raw_location.startswith("////"):
        raise ExchangeContractError("canonical image URI must use a triple-slash local form")
    elif path.startswith("/") and len(path) >= 3 and path[2] == ":":
        if not path[1].isalpha() or len(path) < 4 or path[3] != "/":
            raise ExchangeContractError("canonical image URI has an invalid Windows drive path")

    decoded_path = unquote_to_bytes(path)
    if b"\x00" in decoded_path:
        raise ExchangeContractError("canonical image URI path contains NUL")
    if b"\\" in decoded_path:
        raise ExchangeContractError("canonical image URI path contains a separator")
    decoded_segments = decoded_path.split(b"/")
    if any(segment in {b".", b".."} for segment in decoded_segments):
        raise ExchangeContractError("canonical image URI path has a dot segment")
    return value


class _StageResultExchangeContractError(ExchangeContractError):
    """Internal typed result-member failure exposed through the base boundary."""

    def __init__(
        self,
        message: str,
        *,
        code: _StageResultExchangeErrorCode,
    ) -> None:
        super().__init__(message)
        self.code = code


def _nonblank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ROIAnalysisError(f"{field} must be a non-empty string")
    return value


def _sha256(value: object, field: str) -> str:
    text = _nonblank(value, field)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ROIAnalysisError(f"{field} must be a lowercase SHA-256")
    return text


def _safe_segment(value: object, field: str) -> str:
    return validate_safe_path_segment(value, field)


def _freeze_json(value: object, field: str = "value") -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ROIAnalysisError(f"{field} keys must be strings")
        return MappingProxyType(
            {key: _freeze_json(item, f"{field}.{key}") for key, item in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(item, field) for item in value)
    raise ROIAnalysisError(f"{field} must contain only JSON-like values")


def _export_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _export_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export_json(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _export_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _content_digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        content = (
            json.dumps(_export_json(value), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path, error_type: type[ROIAnalysisError]) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise error_type(f"invalid JSON exchange file: {path}") from exc
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise error_type(f"JSON exchange file must contain an object: {path}")
    return value


@dataclass(frozen=True, slots=True)
class SavedProjectState:
    """Native observation of the saved image at result publication.

    We retain the original request and process bindings alongside the saved-data
    checksum. Normal completion and recovered completion compare this receipt
    with the same current project inspection before accepting scientific outputs.
    """

    project_entry_id: str
    saved_state_sha256: str
    prepared_descriptor_sha256: str
    process_launch_sha256: str
    request_envelope_sha256: str

    def __post_init__(self) -> None:
        _nonblank(self.project_entry_id, "saved project entry")
        for name in (
            "saved_state_sha256", "prepared_descriptor_sha256",
            "process_launch_sha256", "request_envelope_sha256",
        ):
            value = getattr(self, name)
            if type(value) is not str or len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ExchangeContractError(f"saved project state {name} is invalid")

    @classmethod
    def from_mapping(cls, value: object) -> "SavedProjectState":
        """Read the single current native receipt with its exact field set."""
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise ExchangeContractError("saved project state receipt fields are invalid")
        return cls(**value)



@dataclass(frozen=True, slots=True)
class ParsedStageResultExchange:
    """Immutable, path-free facts parsed from one producer result exchange."""

    result: StageResult
    result_payload_sha256: str
    result_byte_count: int
    generation_manifest: Mapping[str, Any] | None
    generation_manifest_payload_sha256: str | None
    generation_manifest_byte_count: int | None
    saved_project_state: SavedProjectState


def _reject_duplicate_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ExchangeContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(token: str) -> object:
    raise ExchangeContractError(f"non-finite JSON number is invalid: {token}")


def _finite_json_float(token: str) -> float:
    value = float(token)
    if not isfinite(value):
        raise ExchangeContractError(f"non-finite JSON number is invalid: {token}")
    return value


def _validate_json_bounds(
    value: object,
    *,
    label: str,
    depth: int = 0,
) -> None:
    """Check nesting after the byte-bounded parser has allocated the JSON tree."""
    if depth > _MAX_JSON_DEPTH:
        raise ExchangeContractError(f"{label} exceeds the JSON nesting bound")
    if isinstance(value, Mapping):
        for item in value.values():
            _validate_json_bounds(
                item,
                label=label,
                depth=depth + 1,
            )
    elif type(value) is list:
        for item in value:
            _validate_json_bounds(
                item,
                label=label,
                depth=depth + 1,
            )


def _parse_exchange_object(payload: bytes, label: str) -> Mapping[str, object]:
    if type(payload) is not bytes or not payload or len(payload) > _MAX_EXCHANGE_BYTES:
        raise ExchangeContractError(f"{label} payload exceeds its byte bound")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
        )
    except ExchangeContractError:
        raise
    except (
        AttributeError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise ExchangeContractError(f"{label} payload is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise ExchangeContractError(f"{label} payload must be an object")
    _validate_json_bounds(value, label=label)
    return value


def _read_exchange_payload(path: Path, label: str) -> bytes:
    try:
        with path.open("rb") as stream:
            payload = stream.read(_MAX_EXCHANGE_BYTES + 1)
    except OSError as exc:
        raise ExchangeContractError(f"cannot read {label}") from exc
    if not payload or len(payload) > _MAX_EXCHANGE_BYTES:
        raise ExchangeContractError(f"{label} payload exceeds its byte bound")
    return payload


def parse_stage_result_exchange_bytes(
    result_payload: bytes,
    generation_manifest_payload: bytes | None,
    *,
    invocation_id: str,
    run_id: str,
    stage: str,
    slide_id: str,
    generation_manifest_name: str,
    _manifest_required: Callable[[StageResult], bool] | None = None,
) -> ParsedStageResultExchange:
    """Parse one producer result, applying any private pre-authority policy."""

    try:
        envelope = _parse_exchange_object(result_payload, "stage-result")
        required = {
            "schema_version",
            "invocation_id",
            "run_id",
            "stage",
            "slide_id",
            "result",
            "generation_manifest",
            "saved_project_state",
        }
        if set(envelope) != required or envelope.get("schema_version") != RESULT_SCHEMA:
            raise ExchangeContractError("stage-result envelope schema is invalid")
        result = _stage_result_from_mapping(envelope["result"])
        saved_project_state = SavedProjectState.from_mapping(envelope["saved_project_state"])
    except ExchangeContractError as exc:
        raise _StageResultExchangeContractError(
            str(exc),
            code=_StageResultExchangeErrorCode.RESULT_PAYLOAD_INVALID,
        ) from exc

    identity = {
        "invocation_id": invocation_id,
        "run_id": run_id,
        "stage": stage,
        "slide_id": slide_id,
    }
    reference = envelope["generation_manifest"]
    raw_manifest: Mapping[str, object] | None = None
    generation_ids: list[object] | None = None
    if generation_manifest_payload is not None:
        try:
            raw_manifest = _parse_exchange_object(
                generation_manifest_payload, "generation manifest"
            )
            if raw_manifest.get("schema_version") != GENERATION_MANIFEST_SCHEMA:
                raise ExchangeContractError("generation manifest schema_version does not match")
            raw_generation_ids = raw_manifest.get("generation_ids")
            if (
                type(raw_generation_ids) is not list
                or any(type(item) is not str or not item for item in raw_generation_ids)
                or len(set(raw_generation_ids)) != len(raw_generation_ids)
            ):
                raise ExchangeContractError(
                    "generation manifest generation_ids must be unique strings"
                )
            generation_ids = raw_generation_ids
        except ExchangeContractError as exc:
            raise _StageResultExchangeContractError(
                str(exc),
                code=(_StageResultExchangeErrorCode.GENERATION_MANIFEST_PAYLOAD_INVALID),
            ) from exc

    if generation_manifest_payload is None:
        if reference is not None:
            raise _StageResultExchangeContractError(
                "generation manifest bytes are missing",
                code=(_StageResultExchangeErrorCode.RESULT_GENERATION_BINDING_INVALID),
            )
        if _manifest_required is not None and _manifest_required(result) is True:
            raise _StageResultExchangeContractError(
                "stage-result requires a generation manifest",
                code=(_StageResultExchangeErrorCode.GENERATION_MANIFEST_PAYLOAD_INVALID),
            )
        manifest = None
        manifest_sha256 = None
        manifest_byte_count = None
    else:
        assert raw_manifest is not None
        assert generation_ids is not None
        if not isinstance(reference, Mapping) or set(reference) != {
            "path",
            "sha256",
        }:
            raise _StageResultExchangeContractError(
                "generation manifest reference is invalid",
                code=(_StageResultExchangeErrorCode.RESULT_GENERATION_BINDING_INVALID),
            )
        if reference.get("path") != generation_manifest_name:
            raise _StageResultExchangeContractError(
                "generation manifest path is invalid",
                code=(_StageResultExchangeErrorCode.RESULT_GENERATION_BINDING_INVALID),
            )
        if reference.get("sha256") != hashlib.sha256(generation_manifest_payload).hexdigest():
            raise _StageResultExchangeContractError(
                "generation manifest checksum does not match",
                code=(_StageResultExchangeErrorCode.RESULT_GENERATION_BINDING_INVALID),
            )
        if result.generation_id is None or result.generation_id not in generation_ids:
            raise _StageResultExchangeContractError(
                "stage result generation_id is absent from manifest",
                code=(_StageResultExchangeErrorCode.RESULT_GENERATION_BINDING_INVALID),
            )

        for field, expected in identity.items():
            if raw_manifest.get(field) != expected:
                raise _StageResultExchangeContractError(
                    f"generation manifest {field} does not match",
                    code=(_StageResultExchangeErrorCode.RESULT_TARGET_AUTHORITY_INVALID),
                )
        manifest = _freeze_json(raw_manifest, "generation_manifest")
        assert isinstance(manifest, Mapping)
        manifest_sha256 = hashlib.sha256(generation_manifest_payload).hexdigest()
        manifest_byte_count = len(generation_manifest_payload)

    for field, expected in identity.items():
        if envelope.get(field) != expected:
            raise _StageResultExchangeContractError(
                f"stage-result {field} does not match",
                code=(_StageResultExchangeErrorCode.RESULT_TARGET_AUTHORITY_INVALID),
            )
    if result.stage != stage or result.slide_id != slide_id:
        raise _StageResultExchangeContractError(
            "embedded StageResult identity does not match",
            code=_StageResultExchangeErrorCode.RESULT_TARGET_AUTHORITY_INVALID,
        )
    embedded_identity = {
        "stage": stage,
        "slide_id": slide_id,
        "run_id": run_id,
        "generation_id": result.generation_id,
    }
    for member_name, member in (
        ("outputs", result.outputs),
        ("resume_context", result.resume_context),
    ):
        if member is None:
            continue
        for field, expected in embedded_identity.items():
            if field in member and member[field] != expected:
                raise _StageResultExchangeContractError(
                    f"embedded StageResult {member_name}.{field} does not match",
                    code=(_StageResultExchangeErrorCode.RESULT_TARGET_AUTHORITY_INVALID),
                )

    return ParsedStageResultExchange(
        result=result,
        result_payload_sha256=hashlib.sha256(result_payload).hexdigest(),
        result_byte_count=len(result_payload),
        generation_manifest=manifest,
        generation_manifest_payload_sha256=manifest_sha256,
        generation_manifest_byte_count=manifest_byte_count,
        saved_project_state=saved_project_state,
    )


@dataclass(frozen=True, slots=True)
class StageExchangePaths:
    """Canonical files for one QuPath stage invocation."""

    directory: Path
    run_id: str
    stage: str
    invocation_id: str
    slide_id: str

    def __post_init__(self) -> None:
        for field in ("run_id", "stage", "invocation_id"):
            _safe_segment(getattr(self, field), field)
        _nonblank(self.slide_id, "slide_id")

    @classmethod
    def create(
        cls,
        exchange_root: str | Path,
        *,
        run_id: str,
        stage: str,
        invocation_id: str,
        slide_id: str,
    ) -> "StageExchangePaths":
        directory = (
            Path(exchange_root)
            / _safe_segment(run_id, "run_id")
            / _safe_segment(stage, "stage")
            / _safe_segment(invocation_id, "invocation_id")
        )
        directory.mkdir(parents=True, exist_ok=True)
        return cls(directory, run_id, stage, invocation_id, slide_id)

    @classmethod
    def from_directory(cls, directory: str | Path) -> "StageExchangePaths":
        path = Path(directory)
        request = _read_json(path / "request.json", ExchangeContractError)
        _validate_request_envelope(request)
        return cls(
            path,
            request.get("run_id"),
            request.get("stage"),
            request.get("invocation_id"),
            request.get("slide_id"),
        )

    @property
    def request_path(self) -> Path:
        return self.directory / "request.json"

    @property
    def stage_result_path(self) -> Path:
        return self.directory / "stage-result.v1.json"

    @property
    def generation_manifest_path(self) -> Path:
        return self.directory / "generation-manifest.v1.json"

    def write_request(self, request: Mapping[str, Any]) -> None:
        if not isinstance(request, Mapping):
            raise ExchangeContractError("stage request must be an object")
        _validate_request_binding(request)
        envelope = {
            "schema_version": REQUEST_SCHEMA,
            "invocation_id": self.invocation_id,
            "run_id": self.run_id,
            "stage": self.stage,
            "slide_id": self.slide_id,
            "request": _export_json(_freeze_json(request, "request")),
        }
        if self.request_path.exists():
            existing = _read_json(self.request_path, ExchangeContractError)
            if existing == envelope:
                return
            raise ExchangeContractError("conflicting request already exists for this invocation")
        _atomic_write_json(self.request_path, envelope)

    def write_generation_manifest(self, manifest: Mapping[str, Any]) -> str:
        self._validate_manifest(manifest)
        _atomic_write_json(self.generation_manifest_path, manifest)
        return _file_digest(self.generation_manifest_path)

    def read_generation_manifest(self) -> Mapping[str, Any]:
        manifest = _read_json(self.generation_manifest_path, ExchangeContractError)
        self._validate_manifest(manifest)
        return _freeze_json(manifest, "generation_manifest")

    def _validate_manifest(self, manifest: object) -> None:
        if not isinstance(manifest, Mapping):
            raise ExchangeContractError("generation manifest must be an object")
        expected = {
            "schema_version": GENERATION_MANIFEST_SCHEMA,
            "invocation_id": self.invocation_id,
            "run_id": self.run_id,
            "stage": self.stage,
            "slide_id": self.slide_id,
        }
        for field, value in expected.items():
            if manifest.get(field) != value:
                raise ExchangeContractError(f"generation manifest {field} does not match")
        generation_ids = manifest.get("generation_ids")
        if isinstance(generation_ids, (str, bytes)) or not isinstance(generation_ids, Sequence):
            raise ExchangeContractError("generation manifest generation_ids must be a sequence")
        values = tuple(_nonblank(item, "generation_ids") for item in generation_ids)
        if len(set(values)) != len(values):
            raise ExchangeContractError("generation manifest generation_ids must be unique")
        _freeze_json(manifest, "generation_manifest")

    def read_stage_result(self) -> StageResult:
        result_payload = _read_exchange_payload(self.stage_result_path, "stage-result")
        manifest_payload = (
            _read_exchange_payload(self.generation_manifest_path, "generation manifest")
            if self.generation_manifest_path.exists()
            else None
        )
        parsed = parse_stage_result_exchange_bytes(
            result_payload,
            manifest_payload,
            invocation_id=self.invocation_id,
            run_id=self.run_id,
            stage=self.stage,
            slide_id=self.slide_id,
            generation_manifest_name=self.generation_manifest_path.name,
        )
        if parsed.generation_manifest is None and requires_generation_manifest(parsed.result):
            raise ExchangeContractError("stage-result requires a generation manifest")
        return parsed.result



def _stage_result_from_mapping(value: object) -> StageResult:
    if not isinstance(value, Mapping):
        raise ExchangeContractError("embedded result must be an object")
    required = {
        "stage",
        "slide_id",
        "generation_id",
        "status",
        "interaction_reason",
        "affected_object_ids",
        "message",
        "allowed_actions",
        "resume_context",
        "checkpoint_id",
        "outputs",
        "error",
    }
    if set(value) != required:
        raise ExchangeContractError("embedded result fields do not match StageResult")
    for field in ("affected_object_ids", "allowed_actions"):
        if not isinstance(value[field], list):
            raise ExchangeContractError(f"embedded StageResult {field} must be a JSON array")
    try:
        return StageResult.from_mapping(value)
    except (KeyError, TypeError, ValueError, ROIAnalysisError) as exc:
        raise ExchangeContractError("embedded StageResult is invalid") from exc


def _validate_request_envelope(envelope: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "invocation_id",
        "run_id",
        "stage",
        "slide_id",
        "request",
    }
    if set(envelope) != required or envelope.get("schema_version") != REQUEST_SCHEMA:
        raise ExchangeContractError("stage request envelope schema is invalid")
    for field in ("invocation_id", "run_id", "stage", "slide_id"):
        _nonblank(envelope.get(field), field)
    request = envelope.get("request")
    if not isinstance(request, Mapping):
        raise ExchangeContractError("embedded stage request must be an object")
    _validate_request_binding(request)


def _validate_request_binding(request: Mapping[str, Any]) -> None:
    project_file = _nonblank(request.get("project_file"), "project_file")
    if not Path(project_file).is_absolute():
        raise ExchangeContractError("project_file must be absolute")
    _nonblank(request.get("project_entry_id"), "project_entry_id")
    validate_canonical_image_uri(request.get("canonical_image_uri"))
    _sha256(request.get("canonical_image_sha256"), "canonical_image_sha256")
    _sha256(request.get("script_bundle_sha256"), "script_bundle_sha256")
    script_asset_path = request.get("script_asset_path")
    if not isinstance(script_asset_path, str) or not script_asset_path.strip():
        raise ExchangeContractError("script_asset_path must be a non-empty string")
    relative_asset = PurePosixPath(script_asset_path)
    if (
        relative_asset.is_absolute()
        or "\\" in script_asset_path
        or script_asset_path != relative_asset.as_posix()
        or any(part in {"", ".", ".."} for part in relative_asset.parts)
    ):
        raise ExchangeContractError("script_asset_path must be a normalized relative POSIX path")
    _sha256(request.get("script_asset_sha256"), "script_asset_sha256")
    if request.get("exchange_version") != REQUEST_SCHEMA:
        raise ExchangeContractError("stage request exchange_version is invalid")


__all__ = [
    "ExchangeContractError",
    "ParsedStageResultExchange",
    "REQUEST_SCHEMA",
    "StageExchangePaths",
    "parse_stage_result_exchange_bytes",
    "validate_canonical_image_uri",
]
