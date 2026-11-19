"""Strict, versioned JSON wire envelope for spectral evidence.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Any

from .typed_checksum import typed_checksum_sha256

_SCHEMA_NAME = "mif_pipeline.spectral_task6_evidence"
_SCHEMA_VERSION = 3


# ==============================================================================
#                            Reference evidence wire
# ==============================================================================


class Task6WireError(ValueError):
    """A Task-6 wire envelope is malformed, stale, or unsupported."""


def _type_id(value: type) -> str:
    return f"{value.__module__}.{value.__qualname__}"


def _modules() -> tuple[object, ...]:
    from . import (
        background,
        compatibility,
        interactions,
        purification,
        reference_models,
        reference_qc,
    )
    from . import reference_review_bindings, reference_rois, reference_sampling
    from . import final_basis, target_af, curated_library, reference_catalogue

    return (
        background,
        purification,
        reference_qc,
        compatibility,
        interactions,
        reference_models,
        reference_rois,
        reference_sampling,
        reference_review_bindings,
        target_af,
        final_basis,
        curated_library,
        reference_catalogue,
    )


def _registry() -> dict[str, type]:
    from .reference_service_control_preparation import (
        SharedControlPreparationPlan, SampledSharedControlPreparation, ReferenceRoiSampleUse, StainedReferenceRegionChoice, ControlPreparationSetReview,
    )
    from mif_pipeline.io import DigitalRangeAuthority, OffsetAuthority
    from .canonical_reference_input import _canonical_reference_wire_types
    from .reference_service_source_records import (
        BasisSourceDecisionBinding,
        ReferenceControlFilesBinding,
    )
    from .reference_service_roi_purification import (
        ReferenceBackgroundOperation,
        ReferenceRoiPairingSelection,
        ReferenceRoiSelectionPlan,
        ReferenceSignalPurificationOperation,
        SampledReferenceRoiBundle,
    )
    from .reference_service_target_af import (
        TargetAfRoiReviewSelection,
        TargetAfSampleBundle,
        TargetAfSamplingPlan,
        TargetAfSharedRoiAuthority,
        TargetAfRetainedRoiAuthority,
        TargetAfSourceReviewSelection,
        TargetControlReviewResponse,
    )

    result: dict[str, type] = {}
    for module in _modules():
        for value in vars(module).values():
            if not isinstance(value, type) or value.__module__ != module.__name__:
                continue
            if is_dataclass(value) or issubclass(value, Enum):
                result[_type_id(value)] = value
    for value in (DigitalRangeAuthority, OffsetAuthority):
        result[_type_id(value)] = value
    for value in _canonical_reference_wire_types():
        result[_type_id(value)] = value
    for value in (
        SharedControlPreparationPlan, SampledSharedControlPreparation, ReferenceRoiSampleUse, StainedReferenceRegionChoice, ControlPreparationSetReview,
        BasisSourceDecisionBinding,
        ReferenceControlFilesBinding,
        ReferenceRoiPairingSelection,
        ReferenceRoiSelectionPlan,
        ReferenceBackgroundOperation,
        ReferenceSignalPurificationOperation,
        SampledReferenceRoiBundle,
        TargetAfRoiReviewSelection,
        TargetAfSamplingPlan,
        TargetAfSampleBundle,
        TargetAfSharedRoiAuthority,
        TargetAfRetainedRoiAuthority,
        TargetAfSourceReviewSelection,
        TargetControlReviewResponse,
    ):
        result[_type_id(value)] = value
    return result


def _task6_top_level_types() -> frozenset[type]:
    from . import background, purification, reference_qc
    from .reference_service_roi_purification import (
        ReferenceBackgroundOperation,
        ReferenceRoiSelectionPlan,
        ReferenceSignalPurificationOperation,
        SampledReferenceRoiBundle,
    )
    from .reference_service_target_af import (
        TargetAfRoiReviewSelection,
        TargetAfSampleBundle,
        TargetAfSamplingPlan,
        TargetAfSharedRoiAuthority,
        TargetControlReviewResponse,
    )

    result: set[type] = set()
    for module in (background, purification, reference_qc):
        for name in module.__all__:
            value = getattr(module, name)
            if isinstance(value, type) and is_dataclass(value):
                result.add(value)
    result.update(
        {
            ReferenceRoiSelectionPlan,
            SampledReferenceRoiBundle,
            ReferenceBackgroundOperation,
            ReferenceSignalPurificationOperation,
            TargetAfRoiReviewSelection,
            TargetAfSamplingPlan,
            TargetAfSampleBundle,
            TargetAfSharedRoiAuthority,
            TargetControlReviewResponse,
        }
    )
    return frozenset(result)


def _encode(value: object) -> dict[str, object]:
    """Encode one value using a single registry for its nested records."""
    return _encode_value(value, _registry())


def _encode_value(value: object, registry: dict[str, type]) -> dict[str, object]:
    """Encode nested values with the calling operation's allowed record types."""
    if value is None:
        return {"kind": "none"}
    if type(value) is bool:
        return {"kind": "bool", "value": value}
    if type(value) is int:
        return {"kind": "int", "value": str(value)}
    if type(value) is float:
        if not (-float("inf") < value < float("inf")):
            raise Task6WireError("wire floats must be finite")
        return {"kind": "float", "value": value.hex()}
    if type(value) is str:
        return {"kind": "str", "value": value}
    if isinstance(value, Path):
        return {"kind": "path", "value": str(value)}
    if isinstance(value, Enum):
        return {
            "kind": "enum",
            "type": _type_id(type(value)),
            "value": _encode_value(value.value, registry),
        }
    if type(value) is tuple:
        return {"kind": "tuple", "items": [_encode_value(item, registry) for item in value]}
    if is_dataclass(value) and not isinstance(value, type):
        from .reference_models import _record_fields
        record_type = type(value)
        if _type_id(record_type) not in registry:
            raise Task6WireError(f"unsupported record type {_type_id(record_type)!r}")
        return {
            "kind": "record",
            "type": _type_id(record_type),
            "fields": {
                item.name: _encode_value(getattr(value, item.name), registry)
                for item in _record_fields(value)
                if not (
                    _type_id(record_type) == "mif_pipeline.preprocessing.unmixing.final_basis.FinalRunBasisProposal"
                    and item.name == "af_behavior" and value.af_behavior == "retain"
                )
            },
        }
    raise Task6WireError(f"unsupported wire value {type(value).__name__}")


def _mapping(value: object, expected: frozenset[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise Task6WireError(f"{label} must contain exactly {sorted(expected)!r}")
    return value


def _decode(value: object, registry: dict[str, type]) -> object:
    if type(value) is not dict or type(value.get("kind")) is not str:
        raise Task6WireError("wire value must be an exactly tagged object")
    kind = value["kind"]
    if kind == "none":
        _mapping(value, frozenset({"kind"}), "none value")
        return None
    if kind == "bool":
        item = _mapping(value, frozenset({"kind", "value"}), "bool value")["value"]
        if type(item) is not bool:
            raise Task6WireError("bool value must contain an exact boolean")
        return item
    if kind == "int":
        item = _mapping(value, frozenset({"kind", "value"}), "int value")["value"]
        if (
            type(item) is not str
            or not item
            or (item.startswith("-") and not item[1:].isdigit())
            or (not item.startswith("-") and not item.isdigit())
        ):
            raise Task6WireError("int value must contain canonical decimal text")
        parsed = int(item)
        if str(parsed) != item:
            raise Task6WireError("int value is not canonical decimal text")
        return parsed
    if kind == "float":
        item = _mapping(value, frozenset({"kind", "value"}), "float value")["value"]
        if type(item) is not str:
            raise Task6WireError("float value must contain hexadecimal text")
        try:
            parsed = float.fromhex(item)
        except ValueError as exc:
            raise Task6WireError("invalid hexadecimal float") from exc
        if not (-float("inf") < parsed < float("inf")) or parsed.hex() != item:
            raise Task6WireError("float value is nonfinite or noncanonical")
        return parsed
    if kind in {"str", "path"}:
        item = _mapping(value, frozenset({"kind", "value"}), f"{kind} value")["value"]
        if type(item) is not str:
            raise Task6WireError(f"{kind} value must contain exact text")
        return item if kind == "str" else Path(item)
    if kind == "tuple":
        items = _mapping(value, frozenset({"kind", "items"}), "tuple value")["items"]
        if type(items) is not list:
            raise Task6WireError("tuple items must be an array")
        return tuple(_decode(item, registry) for item in items)
    if kind == "enum":
        item = _mapping(value, frozenset({"kind", "type", "value"}), "enum value")
        type_id = item["type"]
        if type(type_id) is not str or type_id not in registry:
            raise Task6WireError("unknown enum type")
        enum_type = registry[type_id]
        if not issubclass(enum_type, Enum):
            raise Task6WireError("declared enum type is not an enum")
        try:
            return enum_type(_decode(item["value"], registry))
        except (TypeError, ValueError) as exc:
            raise Task6WireError("invalid enum value") from exc
    if kind == "record":
        item = _mapping(value, frozenset({"kind", "type", "fields"}), "record value")
        type_id = item["type"]
        if type(type_id) is not str or type_id not in registry:
            raise Task6WireError("unknown record type")
        record_type = registry[type_id]
        if not is_dataclass(record_type):
            raise Task6WireError("declared record type is not a dataclass")
        serialized_fields = item["fields"]
        expected_names = frozenset(field.name for field in fields(record_type))
        implicit_retain = (
            type_id == "mif_pipeline.preprocessing.unmixing.final_basis.FinalRunBasisProposal"
            and type(serialized_fields) is dict and "af_behavior" not in serialized_fields
        )
        if implicit_retain:
            expected_names -= {"af_behavior"}
        implicit_optical = (
            type_id == "mif_pipeline.preprocessing.unmixing.reference_models.SpectralMeasurement"
            and type(serialized_fields) is dict and "optical_setting" not in serialized_fields
        )
        if implicit_optical:
            expected_names -= {"optical_setting"}
        implicit_optional = {item.name: item.default for item in fields(record_type)
                             if item.metadata.get("omit_empty")
                             and type(serialized_fields) is dict and item.name not in serialized_fields}
        expected_names -= implicit_optional.keys()
        decoded_mapping = _mapping(serialized_fields, expected_names, f"{type_id} fields")
        decoded = {
            name: _decode(field_value, registry) for name, field_value in decoded_mapping.items()
        }
        decoded.update(implicit_optional)
        if implicit_retain:
            decoded["af_behavior"] = "retain"
        if implicit_optical:
            decoded["optical_setting"] = None
        init_values = {
            field.name: decoded[field.name] for field in fields(record_type) if field.init
        }
        try:
            record = record_type(**init_values)
        except (AssertionError, TypeError, ValueError) as exc:
            raise Task6WireError(f"invalid {type_id} evidence") from exc
        if any(getattr(record, field.name) != decoded[field.name] for field in fields(record_type)):
            raise Task6WireError(f"derived fields in {type_id} are stale")
        return record
    raise Task6WireError(f"unsupported wire kind {kind!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Task6WireError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def dumps_task6_evidence(record: object) -> str:
    """Serialize one exact public record to a checksummed JSON envelope."""

    if type(record) not in _task6_top_level_types():
        raise TypeError("record must be an exact public Task-6 dataclass")
    encoded = _encode(record)
    payload = {
        "schema_name": _SCHEMA_NAME,
        "schema_version": _SCHEMA_VERSION,
        "record": encoded,
    }
    envelope = {
        **payload,
        "content_sha256": typed_checksum_sha256(payload),
    }
    return (
        json.dumps(
            envelope,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def loads_task6_evidence(source: str | bytes) -> object:
    """Parse and fully revalidate one strict evidence envelope."""

    if not isinstance(source, (str, bytes)):
        raise TypeError("source must be str or bytes")
    try:
        root = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                Task6WireError(f"nonfinite JSON constant {value!r}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Task6WireError("invalid Task-6 JSON") from exc
    envelope = _mapping(
        root,
        frozenset({"schema_name", "schema_version", "record", "content_sha256"}),
        "Task-6 envelope",
    )
    if (
        envelope["schema_name"] == _SCHEMA_NAME
        and type(envelope["schema_version"]) is int
        and envelope["schema_version"] in (1, 2)
    ):
        raise Task6WireError("Task-6 evidence versions 1 and 2 require source-backed migration")
    if (
        envelope["schema_name"] != _SCHEMA_NAME
        or type(envelope["schema_version"]) is not int
        or envelope["schema_version"] != _SCHEMA_VERSION
    ):
        raise Task6WireError("unsupported Task-6 wire schema")
    declared = envelope["content_sha256"]
    if type(declared) is not str:
        raise Task6WireError("content_sha256 must be text")
    payload = {
        "schema_name": envelope["schema_name"],
        "schema_version": envelope["schema_version"],
        "record": envelope["record"],
    }
    if typed_checksum_sha256(payload) != declared:
        raise Task6WireError("Task-6 wire content SHA-256 does not match")
    record = _decode(envelope["record"], _registry())
    if type(record) not in _task6_top_level_types():
        raise Task6WireError("wire root is not a public Task-6 record")
    return record


__all__ = ["Task6WireError", "dumps_task6_evidence", "loads_task6_evidence"]
