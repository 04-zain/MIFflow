"""Neutral state-issued interaction authority for batch training consumers.

The `AuthorizedTrainingInteractionResponse` record carries `current_execution`,
`accepted_response`, `checkpoint_id`.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import math
from types import MappingProxyType
from typing import TypeAlias

from mif_pipeline.pipeline_core import ROIAnalysisError

from .batch_artifacts import (
    BatchArtifactError,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    validate_current_execution_authority,
)

JsonValue: TypeAlias = (
    None | bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
)
_AUTHORIZED_TRAINING_INTERACTION_RESPONSE_ISSUER = object()
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_RESPONSE_FIELDS = frozenset(
    {
        "schema_name",
        "schema_version",
        "target_id",
        "group_id",
        "group_sha256",
        "node_id",
        "prior_attempt_id",
        "prior_attempt_authorization_sha256",
        "prior_terminal_record_sha256",
        "interaction_request_id",
        "interaction_context_sha256",
        "action",
        "response",
        "response_validator_id",
        "response_validator_version",
        "response_sha256",
    }
)


# ==============================================================================
#                          Batch interaction authority
# ==============================================================================


class BatchInteractionAuthorityError(ROIAnalysisError):
    """Invalid or mutated state-issued training interaction authority."""


def _closed_mapping(
    value: object,
    *,
    fields: frozenset[str],
    field: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BatchInteractionAuthorityError(f"{field} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise BatchInteractionAuthorityError(f"{field} keys must be strings")
    actual = frozenset(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise BatchInteractionAuthorityError(f"{field} fields are invalid: {'; '.join(details)}")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BatchInteractionAuthorityError(f"{field} must be a non-empty string")
    return value


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BatchInteractionAuthorityError(f"{field} must be a lowercase SHA-256")
    return value


def _freeze_json(value: object, field: str) -> JsonValue:
    if value is None or type(value) is bool or isinstance(value, str):
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise BatchInteractionAuthorityError(f"{field} must contain finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise BatchInteractionAuthorityError(f"{field} keys must be non-empty strings")
            frozen[key] = _freeze_json(item, f"{field}.{key}")
        return MappingProxyType(dict(sorted(frozen.items())))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, f"{field}[{index}]") for index, item in enumerate(value))
    raise BatchInteractionAuthorityError(f"{field} must contain only finite JSON values")


def _export_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _export_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export_json(item) for item in value]
    return value


def _digest(value: object) -> str:
    try:
        payload = json.dumps(
            _export_json(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise BatchInteractionAuthorityError(
            "authorized training interaction material must be canonical JSON"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _is_deeply_frozen_json(value: object) -> bool:
    if value is None or type(value) in {bool, int, float, str}:
        return True
    if type(value) is tuple:
        return all(_is_deeply_frozen_json(item) for item in value)
    if type(value) is _MAPPING_PROXY_TYPE:
        return all(
            type(key) is str and _is_deeply_frozen_json(item)
            for key, item in value.items()  # type: ignore[union-attr]
        )
    return False


def _validated_response_mapping(
    value: object,
    *,
    require_frozen: bool,
) -> Mapping[str, JsonValue]:
    mapping = _closed_mapping(
        value,
        fields=_RESPONSE_FIELDS,
        field="authorized training interaction response",
    )
    if require_frozen and (
        type(mapping) is not _MAPPING_PROXY_TYPE or not _is_deeply_frozen_json(mapping)
    ):
        raise BatchInteractionAuthorityError(
            "authorized training interaction response is not deeply immutable"
        )
    frozen = _freeze_json(mapping, "authorized training interaction response")
    assert isinstance(frozen, Mapping)
    if (
        frozen["schema_name"] != "mif-batch-interaction-response"
        or type(frozen["schema_version"]) is not int
        or frozen["schema_version"] != 1
    ):
        raise BatchInteractionAuthorityError(
            "authorized training interaction response schema is not current"
        )
    for field_name in (
        "group_sha256",
        "prior_attempt_authorization_sha256",
        "prior_terminal_record_sha256",
        "interaction_context_sha256",
        "response_sha256",
    ):
        _sha256(frozen[field_name], field_name)
    for field_name in (
        "target_id",
        "group_id",
        "node_id",
        "prior_attempt_id",
        "interaction_request_id",
        "action",
        "response_validator_id",
        "response_validator_version",
    ):
        _text(frozen[field_name], field_name)
    unsigned = {key: item for key, item in frozen.items() if key != "response_sha256"}
    if frozen["response_sha256"] != _digest(unsigned):
        raise BatchInteractionAuthorityError(
            "authorized training interaction response checksum differs"
        )
    return frozen


@dataclass(frozen=True, slots=True, init=False)
class AuthorizedTrainingInteractionResponse:
    """Opaque state-issued capability for one exact accepted training retry."""

    current_execution: BatchCurrentExecutionAuthority
    accepted_response: Mapping[str, JsonValue]
    checkpoint_id: str
    _seal_sha256: str
    _issuer: object
    _current_execution_binding: object = field(repr=False, compare=False)
    _accepted_response_binding: object = field(repr=False, compare=False)

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("AuthorizedTrainingInteractionResponse is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("AuthorizedTrainingInteractionResponse cannot be subclassed")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("AuthorizedTrainingInteractionResponse is nonserializable")

    @property
    def response_sha256(self) -> str:
        return str(self.accepted_response["response_sha256"])

    @property
    def action(self) -> str:
        return str(self.accepted_response["action"])

    @property
    def response(self) -> JsonValue:
        return self.accepted_response["response"]


def _authorized_response_seal_material(
    *,
    authority: AuthorizedTrainingInteractionResponse,
    current_execution: BatchCurrentExecutionAuthority,
    accepted_response: Mapping[str, JsonValue],
    checkpoint_id: str,
) -> dict[str, object]:
    return {
        "authority_object_identity": id(authority),
        "current_execution_object_identity": id(current_execution),
        "accepted_response_object_identity": id(accepted_response),
        "current_execution_authority_seal_sha256": (current_execution._seal_sha256),
        "accepted_response": _export_json(accepted_response),
        "checkpoint_id": checkpoint_id,
    }


def _issue_authorized_training_interaction_response(
    *,
    current_execution: BatchCurrentExecutionAuthority,
    accepted_response: Mapping[str, object],
    checkpoint_id: str,
    expected_context: BatchNodeExecutionContext,
) -> AuthorizedTrainingInteractionResponse:
    frozen_response = _validated_response_mapping(
        accepted_response,
        require_frozen=False,
    )
    checkpoint = _text(checkpoint_id, "interaction checkpoint_id")
    authority = object.__new__(AuthorizedTrainingInteractionResponse)
    values = {
        "current_execution": current_execution,
        "accepted_response": frozen_response,
        "checkpoint_id": checkpoint,
        "_seal_sha256": _digest(
            _authorized_response_seal_material(
                authority=authority,
                current_execution=current_execution,
                accepted_response=frozen_response,
                checkpoint_id=checkpoint,
            )
        ),
        "_issuer": _AUTHORIZED_TRAINING_INTERACTION_RESPONSE_ISSUER,
        "_current_execution_binding": current_execution,
        "_accepted_response_binding": frozen_response,
    }
    for field_name in AuthorizedTrainingInteractionResponse.__dataclass_fields__:
        object.__setattr__(authority, field_name, values[field_name])
    return _validate_authorized_training_interaction_response(
        authority,
        expected_context=expected_context,
    )


def _validate_authorized_training_interaction_response(
    authority: AuthorizedTrainingInteractionResponse,
    *,
    expected_context: BatchNodeExecutionContext,
) -> AuthorizedTrainingInteractionResponse:
    """Validate authorized training interaction response for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if (
        type(authority) is not AuthorizedTrainingInteractionResponse
        or authority._issuer is not _AUTHORIZED_TRAINING_INTERACTION_RESPONSE_ISSUER
    ):
        raise BatchInteractionAuthorityError(
            "authorized training interaction response issuer is invalid"
        )
    if type(expected_context) is not BatchNodeExecutionContext:
        raise BatchInteractionAuthorityError(
            "authorized training interaction response requires exact context"
        )
    try:
        validate_current_execution_authority(
            authority.current_execution,
            expected_context=expected_context,
        )
    except (BatchArtifactError, AttributeError, TypeError, ValueError) as exc:
        raise BatchInteractionAuthorityError(
            "authorized training interaction response context differs"
        ) from exc
    response = _validated_response_mapping(
        authority.accepted_response,
        require_frozen=True,
    )
    if authority.accepted_response is not authority._accepted_response_binding:
        raise BatchInteractionAuthorityError(
            "authorized training interaction response mapping was transplanted"
        )
    if authority.current_execution is not authority._current_execution_binding:
        raise BatchInteractionAuthorityError(
            "authorized training interaction execution authority was transplanted"
        )
    checkpoint = _text(authority.checkpoint_id, "interaction checkpoint_id")
    authorization = expected_context.authorization
    if (
        response["node_id"] != expected_context.node_id
        or response["response_sha256"] != authorization.interaction_response_sha256
        or response["prior_terminal_record_sha256"]
        != authorization.interaction_predecessor_terminal_record_sha256
        or authorization.retry_ordinal <= 0
    ):
        raise BatchInteractionAuthorityError(
            "authorized training interaction response lineage differs"
        )
    expected_seal = _digest(
        _authorized_response_seal_material(
            authority=authority,
            current_execution=authority.current_execution,
            accepted_response=authority.accepted_response,
            checkpoint_id=checkpoint,
        )
    )
    if _sha256(authority._seal_sha256, "authorized response seal") != expected_seal:
        raise BatchInteractionAuthorityError(
            "authorized training interaction response seal is invalid"
        )
    return authority


__all__ = ["AuthorizedTrainingInteractionResponse"]
