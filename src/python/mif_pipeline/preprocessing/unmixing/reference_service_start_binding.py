"""Private exact target-input authority binding for reference service starts.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from .reference_models import _record_fields
from dataclasses import fields, is_dataclass
from enum import Enum
import hashlib
import json

from .reference_models import ReferenceInputEvidence
from .reference_store_fs import (
    ReferenceCheckpointStoreError,
    _validate_sha256,
)
from .reference_workflow import ReferenceWorkflowCheckpoint

_START_SCHEMA_NAME = "mif_pipeline.spectral_reference_start_authority"
_START_SCHEMA_VERSION = 2
_MAX_START_AUTHORITY_BYTES = 65_536


# ==============================================================================
#                        Reference service start binding
# ==============================================================================


def _authority_value(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not (-float("inf") < value < float("inf")):
            raise ReferenceCheckpointStoreError("target authority contains a nonfinite number")
        return {"float_hex": value.hex()}
    if isinstance(value, Enum):
        return {
            "enum_type": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": _authority_value(value.value),
        }
    if type(value) is tuple:
        return [_authority_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "record_type": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": {
                item.name: _authority_value(getattr(value, item.name)) for item in _record_fields(value)
            },
        }
    raise ReferenceCheckpointStoreError(
        f"unsupported target authority value {type(value).__name__}"
    )


def _authority_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            _authority_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _revalidate_target_input(
    target_input: ReferenceInputEvidence,
) -> ReferenceInputEvidence:
    if type(target_input) is not ReferenceInputEvidence:
        raise TypeError("target input must be exact ReferenceInputEvidence")
    reconstructed = ReferenceInputEvidence(
        **{item.name: getattr(target_input, item.name) for item in fields(target_input)}
    )
    if reconstructed != target_input:
        raise ReferenceCheckpointStoreError(
            "target input differs after exact record reconstruction"
        )
    if reconstructed.input_role != "target_image":
        raise ValueError("start requires target input")
    if (
        reconstructed.structurally_compatible is False
        and reconstructed.structural_compatibility_evidence_sha256 is not None
    ):
        raise ValueError("target without structural comparison must lack report hash")
    if not reconstructed.quantitatively_transformable:
        raise ValueError("target input must be quantitatively transformable")
    return reconstructed


def _start_authority_bytes(
    run_sha256: str, target_input: ReferenceInputEvidence
) -> tuple[bytes, str]:
    target = _revalidate_target_input(target_input)
    content: dict[str, object] = {
        "schema_name": _START_SCHEMA_NAME,
        "schema_version": _START_SCHEMA_VERSION,
        "run_sha256": run_sha256,
        "target_input_id": target.input_id,
        "target_image_sha256": target.source_image_sha256,
        "measurement_ids": list(target.axis.measurement_ids),
        "structurally_compatible": target.structurally_compatible,
        "quantitatively_transformable": target.quantitatively_transformable,
        "structural_signature_sha256": (
            None
            if target.structural_signature is None
            else _authority_digest(target.structural_signature)
        ),
        "quantitative_signature_sha256": _authority_digest(target.quantitative_signature),
        "structural_compatibility_evidence_sha256": (
            target.structural_compatibility_evidence_sha256
        ),
        "quantitative_transform_evidence_sha256": (target.quantitative_transform_evidence_sha256),
        "target_input_sha256": _authority_digest(target),
    }
    content_sha256 = hashlib.sha256(
        json.dumps(
            content,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    raw = json.dumps(
        {**content, "content_sha256": content_sha256},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return raw, content_sha256


def _parse_start_authority_mapping(raw: bytes) -> dict[str, object]:
    def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ReferenceCheckpointStoreError(
                    f"duplicate reference start authority JSON field: {key}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ReferenceCheckpointStoreError(
            f"nonfinite reference start authority value is forbidden: {value}"
        )

    try:
        value = json.loads(
            raw,
            object_pairs_hook=exact_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ReferenceCheckpointStoreError("reference start authority is not strict JSON") from exc
    if type(value) is not dict:
        raise ReferenceCheckpointStoreError("reference start authority must be a JSON object")
    return value


def _parse_start_authority(
    raw: bytes,
    *,
    run_sha256: str,
    checkpoint: ReferenceWorkflowCheckpoint,
) -> str:
    value = _parse_start_authority_mapping(raw)
    expected = {
        "schema_name",
        "schema_version",
        "run_sha256",
        "target_input_id",
        "target_image_sha256",
        "measurement_ids",
        "structurally_compatible",
        "quantitatively_transformable",
        "structural_signature_sha256",
        "quantitative_signature_sha256",
        "structural_compatibility_evidence_sha256",
        "quantitative_transform_evidence_sha256",
        "target_input_sha256",
        "content_sha256",
    }
    if (
        value.get("schema_name") == _START_SCHEMA_NAME
        and type(value.get("schema_version")) is int
        and value["schema_version"] == 1
    ):
        raise ReferenceCheckpointStoreError(
            "legacy reference start authority requires explicit source-backed migration"
        )
    if (
        set(value) != expected
        or (value["schema_name"], value["schema_version"])
        != (_START_SCHEMA_NAME, _START_SCHEMA_VERSION)
        or type(value["schema_version"]) is not int
    ):
        raise ReferenceCheckpointStoreError("reference start authority schema is invalid")
    if value["run_sha256"] != run_sha256:
        raise ReferenceCheckpointStoreError("reference start authority crosses run identity")
    if value["target_image_sha256"] != checkpoint.target_image_sha256:
        raise ReferenceCheckpointStoreError(
            "reference start authority crosses target image identity"
        )
    if value["measurement_ids"] != list(checkpoint.measurement_ids):
        raise ReferenceCheckpointStoreError(
            "reference start authority crosses measurement identity"
        )
    if value["quantitatively_transformable"] is not True:
        raise ReferenceCheckpointStoreError(
            "reference start authority is not compatible and transformable"
        )
    absent_report = (
        value["structurally_compatible"] is False
        and value["structural_compatibility_evidence_sha256"] is None
    )
    compared_report = (
        value["structurally_compatible"] is True
        and value["structural_signature_sha256"] is not None
        and value["structural_compatibility_evidence_sha256"] is not None
    )
    if not (absent_report or compared_report):
        raise ReferenceCheckpointStoreError("reference start structural authority is inconsistent")
    for name in (
        "quantitative_signature_sha256",
        "quantitative_transform_evidence_sha256",
        "target_input_sha256",
        "content_sha256",
    ):
        _validate_sha256(value[name], f"reference start authority {name}")
    if value["structural_signature_sha256"] is not None:
        _validate_sha256(
            value["structural_signature_sha256"],
            "reference start authority structural_signature_sha256",
        )
    if compared_report:
        _validate_sha256(
            value["structural_compatibility_evidence_sha256"],
            "reference start authority structural_compatibility_evidence_sha256",
        )
    if type(value["target_input_id"]) is not str or not value["target_input_id"]:
        raise ReferenceCheckpointStoreError("reference start input ID is invalid")
    content = {key: item for key, item in value.items() if key != "content_sha256"}
    expected_sha256 = hashlib.sha256(
        json.dumps(
            content,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if value["content_sha256"] != expected_sha256:
        raise ReferenceCheckpointStoreError("reference start authority checksum differs")
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if canonical != raw:
        raise ReferenceCheckpointStoreError("reference start authority bytes are not canonical")
    return expected_sha256


__all__: list[str] = []
