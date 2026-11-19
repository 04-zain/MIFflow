"""Bounded, spawn-safe wire records for sample-local preprocessing.

Only scalar identities and canonical JSON bytes cross the process boundary. Readers, OME
models, arrays, AF models, and mutable checkpoints remain in the worker that owns one
scientific invocation.

The `PreprocessingJobRequest` record carries `schema_name`, `schema_version`, `node_id`,
`node_kind`, `node_version` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import multiprocessing
import re
from typing import Protocol, runtime_checkable

from .batch_artifacts import BatchNodeExecutionContext

PREPROCESSING_JOB_REQUEST_SCHEMA_NAME = "mif-batch-preprocessing-job-request"
PREPROCESSING_JOB_RESULT_SCHEMA_NAME = "mif-batch-preprocessing-job-result"
PREPROCESSING_JOB_SCHEMA_VERSION = 1

_WIRE_JSON_LIMIT = 1_048_576
_DIAGNOSTICS_LIMIT = 65_536
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


# ==============================================================================
#                            Batch preprocessing wire
# ==============================================================================


def _plain_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("preprocessing JSON object keys must be strings")
            result[key] = _plain_json_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_plain_json_value(item) for item in value]
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise ValueError("preprocessing wire value contains a non-JSON leaf")


def canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            _plain_json_value(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("preprocessing wire value is not finite canonical JSON") from exc


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def require_sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def require_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty canonical text")
    return value


def _canonical_json_field(value: object, field: str, maximum: int) -> bytes:
    if type(value) is not bytes:
        raise TypeError(f"{field} must be bytes")
    if not value or len(value) > maximum:
        raise ValueError(f"{field} exceeds its bounded size")
    try:
        decoded = json.loads(
            value.decode("utf-8"),
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"nonfinite value {item!r}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} must be UTF-8 JSON") from exc
    if canonical_bytes(decoded) != value:
        raise ValueError(f"{field} must contain canonical JSON bytes")
    return value


def decode_json(value: bytes, field: str) -> object:
    _canonical_json_field(value, field, _WIRE_JSON_LIMIT)
    return json.loads(value.decode("utf-8"))


@dataclass(frozen=True, slots=True)
class PreprocessingJobRequest:
    schema_name: str
    schema_version: int
    node_id: str
    node_kind: str
    node_version: str
    node_material_sha256: str
    adapter_spec_sha256: str
    authorization_sha256: str
    resolved_config_sha256: str
    node_json: bytes
    authorization_json: bytes
    resolved_sample_json: bytes
    input_artifact_refs_json: bytes
    accepted_interaction_response_json: bytes | None

    def __post_init__(self) -> None:
        """Validate the PreprocessingJobRequest fields when the record is constructed.

        The checks cover ``accepted_interaction_response_json``, ``schema_name``,
        ``schema_version``. Invalid field types, values or combinations raise the declared
        validation error before this instance can be used by its caller."""

        if (
            type(self.schema_name) is not str
            or type(self.schema_version) is not int
            or self.schema_name != PREPROCESSING_JOB_REQUEST_SCHEMA_NAME
            or self.schema_version != PREPROCESSING_JOB_SCHEMA_VERSION
        ):
            raise ValueError("preprocessing job request schema is unsupported")
        for field in ("node_id", "node_kind", "node_version"):
            object.__setattr__(self, field, require_text(getattr(self, field), field))
        for field in (
            "node_material_sha256",
            "adapter_spec_sha256",
            "authorization_sha256",
            "resolved_config_sha256",
        ):
            object.__setattr__(self, field, require_sha256(getattr(self, field), field))
        for field in (
            "node_json",
            "authorization_json",
            "resolved_sample_json",
            "input_artifact_refs_json",
        ):
            object.__setattr__(
                self,
                field,
                _canonical_json_field(getattr(self, field), field, _WIRE_JSON_LIMIT),
            )
        response = self.accepted_interaction_response_json
        if response is not None:
            response = _canonical_json_field(
                response,
                "accepted_interaction_response_json",
                _WIRE_JSON_LIMIT,
            )
        object.__setattr__(self, "accepted_interaction_response_json", response)

    def to_mapping(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "PreprocessingJobRequest":
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("preprocessing job request fields differ")
        return cls(**{name: value[name] for name in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class PreprocessingJobResult:
    schema_name: str
    schema_version: int
    node_id: str
    attempt_id: str
    authorization_sha256: str
    stage_result_json: bytes
    output_artifact_refs_json: bytes
    diagnostics_json: bytes

    def __post_init__(self) -> None:
        if (
            type(self.schema_name) is not str
            or type(self.schema_version) is not int
            or self.schema_name != PREPROCESSING_JOB_RESULT_SCHEMA_NAME
            or self.schema_version != PREPROCESSING_JOB_SCHEMA_VERSION
        ):
            raise ValueError("preprocessing job result schema is unsupported")
        object.__setattr__(self, "node_id", require_text(self.node_id, "node_id"))
        object.__setattr__(self, "attempt_id", require_text(self.attempt_id, "attempt_id"))
        object.__setattr__(
            self,
            "authorization_sha256",
            require_sha256(self.authorization_sha256, "authorization_sha256"),
        )
        for field, maximum in (
            ("stage_result_json", _WIRE_JSON_LIMIT),
            ("output_artifact_refs_json", _WIRE_JSON_LIMIT),
            ("diagnostics_json", _DIAGNOSTICS_LIMIT),
        ):
            object.__setattr__(
                self,
                field,
                _canonical_json_field(getattr(self, field), field, maximum),
            )

    def to_mapping(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "PreprocessingJobResult":
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("preprocessing job result fields differ")
        return cls(**{name: value[name] for name in cls.__dataclass_fields__})  # type: ignore[arg-type]


@runtime_checkable
class PreprocessingJobRunner(Protocol):
    def __call__(self, request: PreprocessingJobRequest) -> PreprocessingJobResult: ...


@runtime_checkable
class AuthorizedInteractionResponseResolver(Protocol):
    def __call__(self, context: BatchNodeExecutionContext) -> Mapping[str, object] | None: ...


_numeric_thread_limit = None


def _configure_preprocessing_runtime(java_home: str | None, bioformats_root: str) -> None:
    """Restore parent-selected locations before any child image IO.

    Spawn does not inherit module state. The child IO owner still verifies
    its own JVM and approved JAR inventory; no live Java objects are shared.
    """
    from pathlib import Path
    from threadpoolctl import threadpool_limits
    from mif_pipeline.io import configure_offline_runtime_selection

    # This initializer belongs only to the disposable preprocessing child.
    # Small spectral matrices parallelize across bounded tiles, not nested
    # BLAS teams. Retain the limit for the lifetime of that child process.
    global _numeric_thread_limit
    _numeric_thread_limit = threadpool_limits(limits=1)

    configure_offline_runtime_selection(
        java_home=Path(java_home) if java_home is not None else None,
        bioformats_root=Path(bioformats_root),
    )


def _run_admitted_preprocessing(
    request: PreprocessingJobRequest, budget
) -> PreprocessingJobResult:
    """Run the image operation with its admitted memory and CPU allowance.

    We pass the small budget record into the spawned process and install it for
    the calculation's lifetime. The image engine reads that allowance to select
    its region batch and numerical thread count. Leaving the operation restores
    the preceding scope, including when image reading or fitting raises an error.
    Direct callers with an empty budget use the image engine's declared defaults.
    """
    from .batch_preprocessing_execution import run_preprocessing_job
    from mif_pipeline.pipeline_core.execution_resources import operation_budget

    if budget is None:
        return run_preprocessing_job(request)
    with operation_budget(budget.memory_bytes, budget.cpu_threads):
        return run_preprocessing_job(request)


@dataclass(frozen=True, slots=True)
class SpawnPreprocessingJobRunner:
    """Execute one node in a fresh spawned process and discard child state."""

    start_method: str = "spawn"

    def __post_init__(self) -> None:
        if self.start_method != "spawn":
            raise ValueError("preprocessing isolation requires spawn context")

    def __call__(self, request: PreprocessingJobRequest) -> PreprocessingJobResult:
        # Local import prevents the wire module from importing scientific services.
        from mif_pipeline.pipeline_core.execution_resources import current_operation_budget

        from mif_pipeline.io import selected_java_home, selected_offline_bioformats_inventory

        java_home = selected_java_home()
        inventory = selected_offline_bioformats_inventory()
        context = multiprocessing.get_context(self.start_method)
        with ProcessPoolExecutor(
            max_workers=1,
            mp_context=context,
            initializer=_configure_preprocessing_runtime,
            initargs=(None if java_home is None else str(java_home), str(inventory.payload_root)),
        ) as executor:
            result = executor.submit(
                _run_admitted_preprocessing, request, current_operation_budget()
            ).result()
        if not isinstance(result, PreprocessingJobResult):
            raise TypeError("preprocessing worker returned an invalid result")
        return result


__all__ = [
    "AuthorizedInteractionResponseResolver",
    "PREPROCESSING_JOB_REQUEST_SCHEMA_NAME",
    "PREPROCESSING_JOB_RESULT_SCHEMA_NAME",
    "PREPROCESSING_JOB_SCHEMA_VERSION",
    "PreprocessingJobRequest",
    "PreprocessingJobResult",
    "PreprocessingJobRunner",
    "SpawnPreprocessingJobRunner",
]
