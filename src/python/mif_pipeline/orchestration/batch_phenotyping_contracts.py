"""Opaque, immutable semantic-profile bindings for batch ROI phenotyping.

The `BatchROISemanticProfileBinding` record carries `validation_domain_id`,
`profile_locator`, `profile_sha256`, `semantic_profile_binding_sha256`,
`profile_schema_name` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import ClassVar
import unicodedata

from .batch_artifacts import ArtifactIdentityContract

MAX_ROI_SEMANTIC_PROFILE_BYTES = 16 * 1024 * 1024

_BINDING_SCHEMA_DISCRIMINATOR = "mif-batch-roi-semantic-profile-binding/v1"
_PROFILE_SCHEMA_NAME = "semantic-profile"
_PROFILE_SCHEMA_VERSION = 3
_TEXT_MAXIMUM_BYTES = 4096
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAPPING_FIELDS = frozenset(
    {
        "schema_version",
        "validation_domain_id",
        "profile_locator",
        "profile_sha256",
        "profile_schema_name",
        "profile_schema_version",
        "semantic_profile_binding_sha256",
    }
)


# ==============================================================================
#                          Batch phenotyping contracts
# ==============================================================================


class BatchPhenotypingContractError(ValueError):
    """Raised when a batch phenotyping contract is not canonical or authentic."""


def semantic_profile_identity_contract(schema_version: int = 4) -> ArtifactIdentityContract:
    """Identify Python-validated semantic configuration, without native execution."""
    if type(schema_version) is not int or schema_version not in {3, 4}:
        raise BatchPhenotypingContractError("unsupported semantic profile schema")
    return ArtifactIdentityContract(
        contract_id="semantic-profile-identity/v2" if schema_version == 3 else "semantic-profile-identity/v3",
        artifact_kind="semantic_profile",
        schema_name="semantic-profile",
        schema_version=schema_version,
        required_fields=("profile_id", "profile_version", "structure_id"),
        optional_fields=(),
        required_runtime_names=("mif_pipeline",),
    )


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BatchPhenotypingContractError(
            "semantic-profile binding material must be canonical JSON"
        ) from exc


def _require_canonical_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchPhenotypingContractError(f"{field} must be nonblank trimmed text")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise BatchPhenotypingContractError(f"{field} must not contain control characters")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BatchPhenotypingContractError(f"{field} must be UTF-8 text") from exc
    if len(encoded) > _TEXT_MAXIMUM_BYTES:
        raise BatchPhenotypingContractError(f"{field} exceeds its UTF-8 byte limit")
    return value


def _require_profile_locator(value: object) -> str:
    locator = _require_canonical_text(value, "profile_locator")
    if locator.startswith("/") or "\\" in locator or ":" in locator or locator.endswith("/"):
        raise BatchPhenotypingContractError("profile_locator must be relative POSIX text")
    segments = locator.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise BatchPhenotypingContractError("profile_locator contains an unsafe path segment")
    if locator != PurePosixPath(locator).as_posix():
        raise BatchPhenotypingContractError("profile_locator must be canonical POSIX text")
    return locator


def _require_sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise BatchPhenotypingContractError(f"{field} must be a lowercase SHA-256")
    return value


def _require_profile_schema(name: object, version: object) -> tuple[str, int]:
    if type(name) is not str or name != _PROFILE_SCHEMA_NAME:
        raise BatchPhenotypingContractError("profile_schema_name must be semantic-profile")
    if type(version) is not int or version not in {3, 4}:
        raise BatchPhenotypingContractError("profile_schema_version must be integer 3 or 4")
    return name, version


def _binding_material(
    validation_domain_id: str,
    profile_locator: str,
    profile_sha256: str,
    profile_schema_name: str,
    profile_schema_version: int,
) -> dict[str, object]:
    return {
        "schema_version": _BINDING_SCHEMA_DISCRIMINATOR,
        "validation_domain_id": validation_domain_id,
        "profile_locator": profile_locator,
        "profile_sha256": profile_sha256,
        "profile_schema_name": profile_schema_name,
        "profile_schema_version": profile_schema_version,
    }


def _binding_sha256(
    validation_domain_id: str,
    profile_locator: str,
    profile_sha256: str,
    profile_schema_name: str,
    profile_schema_version: int,
) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(
            _binding_material(
                validation_domain_id,
                profile_locator,
                profile_sha256,
                profile_schema_name,
                profile_schema_version,
            )
        )
    ).hexdigest()


def _require_profile_bytes(value: object) -> bytes:
    if type(value) is not bytes:
        raise BatchPhenotypingContractError("profile_bytes must be exact bytes")
    if not value or len(value) > MAX_ROI_SEMANTIC_PROFILE_BYTES:
        raise BatchPhenotypingContractError("profile_bytes must be nonempty and bounded")
    return value


@dataclass(frozen=True, slots=True)
class BatchROISemanticProfileBinding:
    validation_domain_id: str
    profile_locator: str
    profile_sha256: str
    semantic_profile_binding_sha256: str
    profile_schema_name: str = _PROFILE_SCHEMA_NAME
    profile_schema_version: int = _PROFILE_SCHEMA_VERSION

    schema_name: ClassVar[str] = "mif-batch-roi-semantic-profile-binding"
    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        validation_domain_id = _require_canonical_text(
            self.validation_domain_id, "validation_domain_id"
        )
        profile_locator = _require_profile_locator(self.profile_locator)
        profile_sha256 = _require_sha256(self.profile_sha256, "profile_sha256")
        profile_schema_name, profile_schema_version = _require_profile_schema(
            self.profile_schema_name, self.profile_schema_version
        )
        semantic_profile_binding_sha256 = _require_sha256(
            self.semantic_profile_binding_sha256, "semantic_profile_binding_sha256"
        )
        expected_digest = _binding_sha256(
            validation_domain_id,
            profile_locator,
            profile_sha256,
            profile_schema_name,
            profile_schema_version,
        )
        if semantic_profile_binding_sha256 != expected_digest:
            raise BatchPhenotypingContractError(
                "semantic_profile_binding_sha256 does not match binding material"
            )
        object.__setattr__(self, "validation_domain_id", validation_domain_id)
        object.__setattr__(self, "profile_locator", profile_locator)
        object.__setattr__(self, "profile_sha256", profile_sha256)
        object.__setattr__(self, "profile_schema_name", profile_schema_name)
        object.__setattr__(self, "profile_schema_version", profile_schema_version)
        object.__setattr__(
            self,
            "semantic_profile_binding_sha256",
            semantic_profile_binding_sha256,
        )

    @classmethod
    def create(
        cls,
        *,
        validation_domain_id: str,
        profile_locator: str,
        profile_bytes: bytes,
        profile_schema_name: str = _PROFILE_SCHEMA_NAME,
        profile_schema_version: int = _PROFILE_SCHEMA_VERSION,
    ) -> "BatchROISemanticProfileBinding":
        validation_domain_id = _require_canonical_text(validation_domain_id, "validation_domain_id")
        profile_locator = _require_profile_locator(profile_locator)
        profile_bytes = _require_profile_bytes(profile_bytes)
        profile_schema_name, profile_schema_version = _require_profile_schema(
            profile_schema_name, profile_schema_version
        )
        profile_sha256 = hashlib.sha256(profile_bytes).hexdigest()
        return cls(
            validation_domain_id=validation_domain_id,
            profile_locator=profile_locator,
            profile_sha256=profile_sha256,
            profile_schema_name=profile_schema_name,
            profile_schema_version=profile_schema_version,
            semantic_profile_binding_sha256=_binding_sha256(
                validation_domain_id,
                profile_locator,
                profile_sha256,
                profile_schema_name,
                profile_schema_version,
            ),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "BatchROISemanticProfileBinding":
        if not isinstance(value, Mapping) or set(value) != _MAPPING_FIELDS:
            raise BatchPhenotypingContractError("semantic-profile binding fields differ")
        if value["schema_version"] != _BINDING_SCHEMA_DISCRIMINATOR:
            raise BatchPhenotypingContractError("semantic-profile binding schema is unsupported")
        return cls(
            validation_domain_id=value["validation_domain_id"],  # type: ignore[arg-type]
            profile_locator=value["profile_locator"],  # type: ignore[arg-type]
            profile_sha256=value["profile_sha256"],  # type: ignore[arg-type]
            profile_schema_name=value["profile_schema_name"],  # type: ignore[arg-type]
            profile_schema_version=value["profile_schema_version"],  # type: ignore[arg-type]
            semantic_profile_binding_sha256=value[
                "semantic_profile_binding_sha256"
            ],  # type: ignore[arg-type]
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            **_binding_material(
                self.validation_domain_id,
                self.profile_locator,
                self.profile_sha256,
                self.profile_schema_name,
                self.profile_schema_version,
            ),
            "semantic_profile_binding_sha256": self.semantic_profile_binding_sha256,
        }


def authenticate_roi_semantic_profile_bytes(
    binding: BatchROISemanticProfileBinding,
    profile_bytes: bytes,
    *,
    expected_validation_domain_id: str,
) -> bytes:
    """Authenticate opaque profile bytes before their owner decodes semantics."""
    if type(binding) is not BatchROISemanticProfileBinding:
        raise BatchPhenotypingContractError("binding must be exact semantic-profile binding")
    profile_bytes = _require_profile_bytes(profile_bytes)
    expected_validation_domain_id = _require_canonical_text(
        expected_validation_domain_id, "expected_validation_domain_id"
    )
    if expected_validation_domain_id != binding.validation_domain_id:
        raise BatchPhenotypingContractError("validation domain does not match binding")
    _require_profile_schema(binding.profile_schema_name, binding.profile_schema_version)
    if hashlib.sha256(profile_bytes).hexdigest() != binding.profile_sha256:
        raise BatchPhenotypingContractError("profile bytes do not match profile_sha256")
    return profile_bytes


__all__ = [
    "semantic_profile_identity_contract",
    "BatchPhenotypingContractError",
    "BatchROISemanticProfileBinding",
    "MAX_ROI_SEMANTIC_PROFILE_BYTES",
    "authenticate_roi_semantic_profile_bytes",
]
