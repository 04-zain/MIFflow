"""Bounded, non-self-contained resume capsules for batch control state.

ZIP is used only as a deterministic transport envelope. Scientific payloads remain external
authorities and the capsule carries only their locators and identity requirements.

The `ResumeCapsuleImportPlan` record carries `capsule_path`, `capsule_sha256`,
`capsule_root_sha256`, `original_manifest_sha256`, `normalized_manifest_sha256` and its
remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import Iterator, Protocol
from uuid import uuid4
import zipfile

from mif_pipeline.orchestration.batch_state import (
    BatchStateError,
    BatchStateHead,
    BatchNodeAttempt,
    BatchStateStore,
    _AuthenticatedResumeCapsuleImport,
    batch_project_workspace_storage_scope_sha256,
    _batch_workspace_rebinding_sha256,
    _batch_request_id,
    _plan_snapshot_from_bytes,
    _resume_import_graph_sha256,
)
from mif_pipeline.orchestration.batch_manifest import BatchManifest, BatchManifestError
from mif_pipeline.orchestration.batch_project import ProjectDesignationRequest
from mif_pipeline.pipeline_core import ROIAnalysisError, StageStatus, assert_current_writer

CAPSULE_SCHEMA_NAME = "mif-batch-resume-capsule"
CAPSULE_SCHEMA_VERSION = 1
SENSITIVE_OPERATIONAL_METADATA_WARNING = (
    "Resume capsules contain sensitive operational metadata and are not a "
    "de-identification boundary. Store and transfer them securely."
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_FIXED_MEMBER_NAMES = frozenset(
    {
        "capsule.json",
        "plan/batch-plan.json",
        "state/head.json",
        "project/identity.json",
        "project/metadata.json",
        "project/authority-head.json",
        "project/latest-mutation-attestation.json",
        "project/identity-materialization-attestation.json",
        "project/catalogue.json",
        "project/training-plan-store/.mif-training-plan-set-store.json",
    }
)
_CONTENT_ADDRESSED_MEMBER_PATTERNS = (
    re.compile(r"manifest/[0-9a-f]{64}/(?:original|normalized)\.json\Z"),
    re.compile(r"project/authority-heads/[0-9a-f]{64}\.json\Z"),
    re.compile(r"project/mutation-attestations/[0-9a-f]{64}\.json\Z"),
    re.compile(r"plan/history/[0-9a-f]{64}\.json\Z"),
    re.compile(r"attempts/authorization-[0-9a-f]{64}\.json\Z"),
    re.compile(r"attempts/terminal-[0-9a-f]{64}\.json\Z"),
    re.compile(r"interactions/response-[0-9a-f]{64}\.json\Z"),
    re.compile(r"state/training-fit-sets/[0-9a-f]{64}\.json\Z"),
    re.compile(r"state/quiescent-pauses/[0-9a-f]{64}\.json\Z"),
    re.compile(r"project/native-fit-materials/[0-9a-f]{64}\.json\Z"),
    re.compile(r"project/plan-runtime-contracts/[0-9a-f]{64}\.json\Z"),
    re.compile(r"project/training-plan-store/objects/[0-9a-f]{64}\.json\Z"),
    re.compile(r"project/secondary-definitions/[0-9a-f]{64}\.json\Z"),
    re.compile(r"project/stage-evidence/[0-9a-f]{64}\.json\Z"),
    re.compile(r"project/catalogues/[0-9a-f]{64}\.json\Z"),
    re.compile(r"project/annotation-publications/[0-9a-f]{32}/(?:ticket\.json|[0-9a-f]{64}/(?:candidate|publication|accepted)\.json)\Z"),
    re.compile(r"project/source-history/[0-9a-f]{64}/(?:project\.qpproj|data/[0-9]+/data\.qpdata)\Z"),
)


# ==============================================================================
#                                  Batch resume
# ==============================================================================


class BatchResumeError(ROIAnalysisError):
    """Raised when a capsule is unsafe, inconsistent, or cannot be imported."""


@dataclass(frozen=True, slots=True, init=False)
class ProjectCapsuleAuthority:
    identity: object
    observed_head: object
    authority_head_history: tuple[object, ...]
    mutation_attestation_history: tuple[object, ...]
    latest_mutation_attestation: object | None
    materialization_attestation: object | None
    catalogue: object | None
    effective_project_path: Path
    _capture_capability: object

    @classmethod
    def _issue(cls, **values: object) -> "ProjectCapsuleAuthority":
        instance = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        return instance


class ProjectCapsuleAuthorityProvider(Protocol):
    @contextmanager
    def capture(
        self,
        *,
        head: BatchStateHead,
        catalogue_store: object,
        workspace_lease: object,
    ) -> Iterator[ProjectCapsuleAuthority]: ...


@dataclass(frozen=True, slots=True, init=False)
class ProjectCapsuleImportSession:
    """Opaque, one-use authority for one authenticated destination import."""

    identity: object
    captured_head: object
    latest_mutation_attestation: object | None
    materialization_attestation: object | None
    catalogue: object | None
    effective_project_path: Path
    destination_head: object
    authenticated_graph_sha256: str
    artifact_inventory: object
    acquisition_record: object
    artifact_resolution_evidence_sha256: str
    _authenticated_graph_capability: _AuthenticatedResumeCapsuleImport
    _catalogue_import_authority: object | None
    _acquisition_session: object
    _session_capability: object
    _prepare_completion_callback: object
    _completion_callback: object
    _finalized: bool

    @classmethod
    def _issue(cls, **values: object) -> "ProjectCapsuleImportSession":
        instance = object.__new__(cls)
        for name in cls.__slots__:
            default = False if name == "_finalized" else None
            object.__setattr__(instance, name, values.get(name, default))
        return instance

    @property
    def is_live(self) -> bool:
        return not self._finalized

    def complete(self, receipt: object) -> None:
        if self._finalized or not callable(self._completion_callback):
            raise BatchResumeError("project capsule import session is not live")
        self._completion_callback(self, receipt)
        object.__setattr__(self, "_finalized", True)

    def prepare_completion(self) -> None:
        if self._finalized or not callable(self._prepare_completion_callback):
            raise BatchResumeError("project capsule import session is not live")
        self._prepare_completion_callback(self)



class ProjectCapsuleImportAuthorityProvider(Protocol):
    @contextmanager
    def open_import_session(
        self,
        *,
        plan: "ResumeCapsuleImportPlan",
        rebindings: tuple["LocatorRebinding", ...],
        validators: "ExternalLocatorValidatorRegistry",
        destination_store: BatchStateStore,
        workspace_lease: object,
        orchestration_bridge: object,
    ) -> Iterator[ProjectCapsuleImportSession]: ...


@dataclass(frozen=True, slots=True, init=False)
class _CapsuleImportDurabilityReceipt:
    receipt_sha256: str
    head_sha256: str
    authenticated_graph_sha256: str
    _session_capability: object

    @classmethod
    def _issue(
        cls,
        *,
        head_sha256: str,
        authenticated_graph_sha256: str,
        session_capability: object,
    ) -> "_CapsuleImportDurabilityReceipt":
        unsigned = {
            "head_sha256": head_sha256,
            "authenticated_graph_sha256": authenticated_graph_sha256,
        }
        instance = object.__new__(cls)
        object.__setattr__(instance, "receipt_sha256", _sha256_bytes(_canonical_bytes(unsigned)))
        object.__setattr__(instance, "head_sha256", head_sha256)
        object.__setattr__(instance, "authenticated_graph_sha256", authenticated_graph_sha256)
        object.__setattr__(instance, "_session_capability", session_capability)
        return instance

    def _validate_project_completion(self, lease: object) -> bool:
        return self._session_capability is lease and self.head_sha256 != ""


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchResumeError(f"{field_name} must be non-empty canonical text")
    return value


def _sha256_text(value: object, field_name: str) -> str:
    digest = _text(value, field_name)
    if _SHA256_RE.fullmatch(digest) is None:
        raise BatchResumeError(f"{field_name} must be a lowercase SHA-256")
    return digest


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise BatchResumeError("capsule material must be finite canonical JSON") from exc


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _freeze_json_tree(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json_tree(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json_tree(item) for item in value)
    return value


def _mutable_json_tree(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _mutable_json_tree(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json_tree(item) for item in value]
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BatchResumeError(f"cannot hash capsule file: {path}") from exc
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ResumeCapsuleLimits:
    max_members: int = 10_000
    max_member_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_compression_ratio: int = 100
    max_json_depth: int = 64

    def __post_init__(self) -> None:
        for name in (
            "max_members",
            "max_member_bytes",
            "max_total_bytes",
            "max_compression_ratio",
            "max_json_depth",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise BatchResumeError(f"{name} must be a positive integer")


DEFAULT_RESUME_CAPSULE_LIMITS = ResumeCapsuleLimits()


@dataclass(frozen=True, slots=True)
class ResumeCapsuleMember:
    name: str
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _allowed_member_name(self.name))
        object.__setattr__(self, "sha256", _sha256_text(self.sha256, "member sha256"))
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise BatchResumeError("member byte_count must be a nonnegative integer")

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
        }


@dataclass(frozen=True, slots=True)
class ExternalLocatorRequirement:
    requirement_id: str
    owner_validator_id: str
    role: str
    original_path: Path
    expected_sha256: str | None
    expected_byte_count: int | None
    owning_identity_sha256: str
    owning_identity: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirement_id", _text(self.requirement_id, "requirement_id"))
        object.__setattr__(
            self, "owner_validator_id", _text(self.owner_validator_id, "owner_validator_id")
        )
        object.__setattr__(self, "role", _text(self.role, "external locator role"))
        if not isinstance(self.original_path, Path) or not self.original_path.is_absolute():
            raise BatchResumeError("external original_path must be an absolute Path")
        if self.expected_sha256 is not None:
            object.__setattr__(
                self,
                "expected_sha256",
                _sha256_text(self.expected_sha256, "external expected_sha256"),
            )
        if self.expected_byte_count is not None and (
            type(self.expected_byte_count) is not int or self.expected_byte_count < 0
        ):
            raise BatchResumeError("external expected_byte_count must be nonnegative")
        object.__setattr__(
            self,
            "owning_identity_sha256",
            _sha256_text(self.owning_identity_sha256, "owning_identity_sha256"),
        )
        if not isinstance(self.owning_identity, Mapping):
            raise BatchResumeError("external owning_identity must be an object")
        frozen = json.loads(_canonical_bytes(dict(self.owning_identity)).decode("utf-8"))
        if not isinstance(frozen, dict):
            raise BatchResumeError("external owning_identity must be an object")
        if _sha256_bytes(_canonical_bytes(frozen)) != self.owning_identity_sha256:
            raise BatchResumeError("external owning identity digest does not match")
        object.__setattr__(self, "owning_identity", _freeze_json_tree(frozen))


@dataclass(frozen=True, slots=True)
class ResumeSampleStatus:
    sample_id: str
    last_durable_node_ids: tuple[str, ...]
    pending_interaction_ids: tuple[str, ...]
    pending_node_ids: tuple[str, ...]
    failed_node_ids: tuple[str, ...]
    completed_node_ids: tuple[str, ...]
    next_eligible_node_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sample_id", _text(self.sample_id, "sample_id"))
        for name in (
            "last_durable_node_ids",
            "pending_interaction_ids",
            "pending_node_ids",
            "failed_node_ids",
            "completed_node_ids",
            "next_eligible_node_ids",
        ):
            values = tuple(_text(item, name) for item in getattr(self, name))
            if len(set(values)) != len(values):
                raise BatchResumeError(f"{name} must not contain duplicates")
            object.__setattr__(self, name, tuple(sorted(values)))


@dataclass(frozen=True, slots=True)
class ResumeDomainStatus:
    domain_id: str
    status: StageStatus | None
    last_durable_node_ids: tuple[str, ...]
    pending_node_ids: tuple[str, ...]
    failed_node_ids: tuple[str, ...]
    completed_node_ids: tuple[str, ...]
    next_eligible_node_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain_id", _text(self.domain_id, "domain_id"))
        if self.status is not None:
            try:
                object.__setattr__(self, "status", StageStatus(self.status))
            except (TypeError, ValueError) as exc:
                raise BatchResumeError("domain status is invalid") from exc
        for name in (
            "last_durable_node_ids",
            "pending_node_ids",
            "failed_node_ids",
            "completed_node_ids",
            "next_eligible_node_ids",
        ):
            values = tuple(_text(item, name) for item in getattr(self, name))
            if len(set(values)) != len(values):
                raise BatchResumeError(f"{name} must not contain duplicates")
            object.__setattr__(self, name, tuple(sorted(values)))


@dataclass(frozen=True, slots=True)
class ExternalIdentityReceipt:
    requirement_id: str
    owner_validator_id: str
    validated_path: Path
    sha256: str | None
    byte_count: int | None
    owning_identity_sha256: str
    receipt_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "requirement_id",
            _text(self.requirement_id, "receipt requirement_id"),
        )
        object.__setattr__(
            self, "owner_validator_id", _text(self.owner_validator_id, "receipt owner_validator_id")
        )
        if not isinstance(self.validated_path, Path) or not self.validated_path.is_absolute():
            raise BatchResumeError("receipt validated_path must be an absolute Path")
        if self.sha256 is not None:
            object.__setattr__(self, "sha256", _sha256_text(self.sha256, "receipt sha256"))
        if self.byte_count is not None and (
            type(self.byte_count) is not int or self.byte_count < 0
        ):
            raise BatchResumeError("receipt byte_count must be nonnegative")
        object.__setattr__(
            self,
            "owning_identity_sha256",
            _sha256_text(self.owning_identity_sha256, "receipt owning_identity_sha256"),
        )
        object.__setattr__(
            self, "receipt_sha256", _sha256_text(self.receipt_sha256, "receipt_sha256")
        )
        if self.receipt_sha256 != _sha256_bytes(_canonical_bytes(self.unsigned_mapping())):
            raise BatchResumeError("external identity receipt checksum does not match")

    def unsigned_mapping(self) -> dict[str, object]:
        return {
            "requirement_id": self.requirement_id,
            "owner_validator_id": self.owner_validator_id,
            "validated_path": str(self.validated_path),
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "owning_identity_sha256": self.owning_identity_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self.unsigned_mapping(), "receipt_sha256": self.receipt_sha256}


@dataclass(frozen=True, slots=True)
class LocatorRebinding:
    requirement_id: str
    replacement_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirement_id", _text(self.requirement_id, "requirement_id"))
        if not isinstance(self.replacement_path, Path):
            raise BatchResumeError("replacement_path must be a Path")


@dataclass(frozen=True, slots=True)
class BatchWorkspaceRebinding:
    original_output_root: Path
    destination_root: Path
    original_manifest_sha256: str
    rebinding_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.original_output_root, Path) or not isinstance(
            self.destination_root, Path
        ):
            raise BatchResumeError("workspace rebinding roots must be Path values")
        if not self.original_output_root.is_absolute() or not self.destination_root.is_absolute():
            raise BatchResumeError("workspace rebinding roots must be absolute")
        object.__setattr__(
            self,
            "original_manifest_sha256",
            _sha256_text(self.original_manifest_sha256, "original_manifest_sha256"),
        )
        object.__setattr__(
            self, "rebinding_sha256", _sha256_text(self.rebinding_sha256, "rebinding_sha256")
        )
        if self.rebinding_sha256 != _batch_workspace_rebinding_sha256(
            self.original_output_root,
            self.destination_root,
            self.original_manifest_sha256,
        ):
            raise BatchResumeError("workspace rebinding checksum does not match")


class ExternalLocatorValidator(Protocol):
    def __call__(
        self, requirement: ExternalLocatorRequirement, replacement_path: Path
    ) -> ExternalIdentityReceipt: ...


@dataclass(frozen=True, slots=True)
class ExternalLocatorValidatorRegistry:
    validators: Mapping[str, ExternalLocatorValidator] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.validators, Mapping):
            raise BatchResumeError("external validator registry must be a mapping")
        normalized: dict[str, ExternalLocatorValidator] = {}
        for key, validator in self.validators.items():
            name = _text(key, "owner validator ID")
            if not callable(validator) or name in normalized:
                raise BatchResumeError("external validator registry is invalid")
            normalized[name] = validator
        object.__setattr__(self, "validators", MappingProxyType(normalized))

    def validate(
        self, requirement: ExternalLocatorRequirement, replacement_path: Path
    ) -> ExternalIdentityReceipt:
        validator = self.validators.get(requirement.owner_validator_id)
        if validator is None:
            raise BatchResumeError(
                f"no owning validator is registered for {requirement.owner_validator_id!r}"
            )
        try:
            receipt = validator(requirement, replacement_path)
        except BatchResumeError:
            raise
        except Exception as exc:
            raise BatchResumeError("owning external locator validation failed") from exc
        if not isinstance(receipt, ExternalIdentityReceipt):
            raise BatchResumeError("owning validator returned an untyped receipt")
        return receipt


@dataclass(frozen=True, slots=True)
class BatchResumeCapsuleManifest:
    schema_name: str
    schema_version: int
    batch_id: str
    original_manifest_sha256: str
    normalized_manifest_sha256: str
    plan_sha256: str
    head_sha256: str
    payload_members: tuple[ResumeCapsuleMember, ...]
    external_locator_requirements: tuple[ExternalLocatorRequirement, ...]
    capsule_root_sha256: str

    def __post_init__(self) -> None:
        """Validate the BatchResumeCapsuleManifest fields when the record is constructed.

        The checks cover ``payload_members``, ``external_locator_requirements``,
        ``capsule_root_sha256``, ``schema_name``, ``schema_version``, ``batch_id``. Invalid
        field types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        if (self.schema_name, self.schema_version) != (
            CAPSULE_SCHEMA_NAME,
            CAPSULE_SCHEMA_VERSION,
        ):
            raise BatchResumeError("resume capsule must use mif-batch-resume-capsule/v1")
        try:
            assert_current_writer(self.schema_name, str(self.schema_version))
        except ValueError as exc:
            raise BatchResumeError("resume capsule schema is not current") from exc
        object.__setattr__(self, "batch_id", _text(self.batch_id, "batch_id"))
        for name in (
            "original_manifest_sha256",
            "normalized_manifest_sha256",
            "plan_sha256",
            "head_sha256",
            "capsule_root_sha256",
        ):
            object.__setattr__(self, name, _sha256_text(getattr(self, name), name))
        members = tuple(self.payload_members)
        if not all(isinstance(item, ResumeCapsuleMember) for item in members):
            raise BatchResumeError("payload_members must contain typed members")
        if members != tuple(sorted(members, key=lambda item: item.name)) or len(
            {item.name.casefold() for item in members}
        ) != len(members):
            raise BatchResumeError("payload member inventory must be canonical and unique")
        if any(item.name == "capsule.json" for item in members):
            raise BatchResumeError("capsule.json cannot inventory itself")
        requirements = tuple(self.external_locator_requirements)
        if not all(isinstance(item, ExternalLocatorRequirement) for item in requirements):
            raise BatchResumeError("external locator requirements must be typed")
        if requirements != tuple(sorted(requirements, key=lambda item: item.requirement_id)) or len(
            {item.requirement_id for item in requirements}
        ) != len(requirements):
            raise BatchResumeError("external locator requirements must be canonical and unique")
        if _capsule_root(members) != self.capsule_root_sha256:
            raise BatchResumeError("capsule root digest does not match payload inventory")
        object.__setattr__(self, "payload_members", members)
        object.__setattr__(self, "external_locator_requirements", requirements)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "batch_id": self.batch_id,
            "original_manifest_sha256": self.original_manifest_sha256,
            "normalized_manifest_sha256": self.normalized_manifest_sha256,
            "plan_sha256": self.plan_sha256,
            "head_sha256": self.head_sha256,
            "payload_members": [item.to_mapping() for item in self.payload_members],
            "external_locator_requirements": [
                _external_requirement_mapping(item) for item in self.external_locator_requirements
            ],
            "capsule_root_sha256": self.capsule_root_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "BatchResumeCapsuleManifest":
        """Decode the serialized fields into a validated BatchResumeCapsuleManifest record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        mapping = _closed_mapping(
            value,
            frozenset(
                {
                    "schema_name",
                    "schema_version",
                    "batch_id",
                    "original_manifest_sha256",
                    "normalized_manifest_sha256",
                    "plan_sha256",
                    "head_sha256",
                    "payload_members",
                    "external_locator_requirements",
                    "capsule_root_sha256",
                }
            ),
            "capsule.json",
        )
        members = tuple(
            ResumeCapsuleMember(
                **_closed_mapping(
                    item,
                    frozenset({"name", "sha256", "byte_count"}),
                    f"payload_members[{index}]",
                )
            )
            for index, item in enumerate(_sequence(mapping["payload_members"], "payload_members"))
        )
        requirements = tuple(
            _external_requirement_from_mapping(item, index=index)
            for index, item in enumerate(
                _sequence(mapping["external_locator_requirements"], "external_locator_requirements")
            )
        )
        return cls(
            schema_name=mapping["schema_name"],
            schema_version=mapping["schema_version"],
            batch_id=mapping["batch_id"],
            original_manifest_sha256=mapping["original_manifest_sha256"],
            normalized_manifest_sha256=mapping["normalized_manifest_sha256"],
            plan_sha256=mapping["plan_sha256"],
            head_sha256=mapping["head_sha256"],
            payload_members=members,
            external_locator_requirements=requirements,
            capsule_root_sha256=mapping["capsule_root_sha256"],
        )


@dataclass(frozen=True, slots=True)
class ResumeCapsuleExport:
    capsule_path: Path
    capsule_sha256: str
    manifest: BatchResumeCapsuleManifest
    member_names: tuple[str, ...]
    sensitive_metadata_warning: str = SENSITIVE_OPERATIONAL_METADATA_WARNING

    def __post_init__(self) -> None:
        if not isinstance(self.capsule_path, Path) or not self.capsule_path.is_absolute():
            raise BatchResumeError("exported capsule_path must be an absolute Path")
        object.__setattr__(
            self,
            "capsule_sha256",
            _sha256_text(self.capsule_sha256, "capsule_sha256"),
        )
        if not isinstance(self.manifest, BatchResumeCapsuleManifest):
            raise BatchResumeError("export descriptor requires a typed capsule manifest")
        try:
            names = tuple(_safe_member_name(name) for name in self.member_names)
        except TypeError as exc:
            raise BatchResumeError("export member_names must be a sequence") from exc
        expected_names = tuple(
            sorted(("capsule.json", *(item.name for item in self.manifest.payload_members)))
        )
        if names != expected_names:
            raise BatchResumeError("export member_names must match the capsule inventory")
        if self.sensitive_metadata_warning != SENSITIVE_OPERATIONAL_METADATA_WARNING:
            raise BatchResumeError("export descriptor must retain the sensitive metadata warning")
        object.__setattr__(self, "member_names", names)


@dataclass(frozen=True, slots=True)
class ResumeCapsuleImportPlan:
    capsule_path: Path
    capsule_sha256: str
    capsule_root_sha256: str
    original_manifest_sha256: str
    normalized_manifest_sha256: str
    plan_sha256: str
    head_sha256: str
    workspace_rebinding: BatchWorkspaceRebinding | None
    locator_requirements: tuple[ExternalLocatorRequirement, ...]
    required_rebindings: tuple[ExternalLocatorRequirement, ...]
    sample_statuses: tuple[ResumeSampleStatus, ...]
    domain_statuses: tuple[ResumeDomainStatus, ...]
    limits: ResumeCapsuleLimits
    member_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate the ResumeCapsuleImportPlan fields when the record is constructed.

        The checks cover ``workspace_rebinding``, ``limits``, ``locator_requirements``,
        ``required_rebindings``, ``sample_statuses``, ``domain_statuses``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        if not isinstance(self.capsule_path, Path) or not self.capsule_path.is_absolute():
            raise BatchResumeError("import capsule_path must be an absolute Path")
        for name in (
            "capsule_sha256",
            "capsule_root_sha256",
            "original_manifest_sha256",
            "normalized_manifest_sha256",
            "plan_sha256",
            "head_sha256",
        ):
            object.__setattr__(self, name, _sha256_text(getattr(self, name), name))
        if self.workspace_rebinding is not None and not isinstance(
            self.workspace_rebinding, BatchWorkspaceRebinding
        ):
            raise BatchResumeError("workspace_rebinding must be typed")
        if not isinstance(self.limits, ResumeCapsuleLimits):
            raise BatchResumeError("import plan limits must be ResumeCapsuleLimits")
        try:
            locator_requirements = tuple(self.locator_requirements)
            requirements = tuple(self.required_rebindings)
            statuses = tuple(self.sample_statuses)
            domain_statuses = tuple(self.domain_statuses)
            names = tuple(_safe_member_name(name) for name in self.member_names)
        except TypeError as exc:
            raise BatchResumeError("import plan collections must be sequences") from exc
        for field_name, values in (
            ("locator_requirements", locator_requirements),
            ("required_rebindings", requirements),
        ):
            if not all(isinstance(item, ExternalLocatorRequirement) for item in values):
                raise BatchResumeError(f"{field_name} must contain typed requirements")
            if values != tuple(sorted(values, key=lambda item: item.requirement_id)) or len(
                {item.requirement_id for item in values}
            ) != len(values):
                raise BatchResumeError(f"{field_name} must be canonical and unique")
        locator_by_id = {item.requirement_id: item for item in locator_requirements}
        if any(locator_by_id.get(item.requirement_id) != item for item in requirements):
            raise BatchResumeError("required_rebindings must be drawn from locator_requirements")
        if not all(isinstance(item, ResumeSampleStatus) for item in statuses):
            raise BatchResumeError("sample_statuses must contain typed statuses")
        if statuses != tuple(sorted(statuses, key=lambda item: item.sample_id)) or len(
            {item.sample_id for item in statuses}
        ) != len(statuses):
            raise BatchResumeError("sample_statuses must be canonical and unique")
        if not all(isinstance(item, ResumeDomainStatus) for item in domain_statuses):
            raise BatchResumeError("domain_statuses must contain typed statuses")
        if domain_statuses != tuple(
            sorted(domain_statuses, key=lambda item: item.domain_id)
        ) or len({item.domain_id for item in domain_statuses}) != len(domain_statuses):
            raise BatchResumeError("domain_statuses must be canonical and unique")
        if names != tuple(sorted(names)) or len({name.casefold() for name in names}) != len(names):
            raise BatchResumeError("member_names must be canonical and unique")
        if names and "capsule.json" not in names:
            raise BatchResumeError("member_names must contain capsule.json")
        object.__setattr__(self, "locator_requirements", locator_requirements)
        object.__setattr__(self, "required_rebindings", requirements)
        object.__setattr__(self, "sample_statuses", statuses)
        object.__setattr__(self, "domain_statuses", domain_statuses)
        object.__setattr__(self, "member_names", names)


def _manifest_member(normalized_sha256: str, part: str) -> str:
    """Name a retained manifest generation using its existing selected digest."""
    return f"manifest/{_sha256_text(normalized_sha256, 'manifest generation')}/{part}.json"


def _safe_member_name(value: object) -> str:
    name = _text(value, "capsule member name")
    if "\\" in name or "\0" in name or re.match(r"^[A-Za-z]:", name):
        raise BatchResumeError("capsule member names cannot contain backslashes, NULs, or drives")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise BatchResumeError("capsule member path must be relative and contained")
    return path.as_posix()


def _allowed_member_name(value: object) -> str:
    name = _safe_member_name(value)
    if name in _FIXED_MEMBER_NAMES or any(
        pattern.fullmatch(name) is not None for pattern in _CONTENT_ADDRESSED_MEMBER_PATTERNS
    ):
        return name
    request_prefix = "interactions/request-"
    request_suffix = ".json"
    if name.startswith(request_prefix) and name.endswith(request_suffix):
        request_id = name[len(request_prefix) : -len(request_suffix)]
        try:
            if _batch_request_id(request_id) == request_id:
                return name
        except BatchStateError as exc:
            raise BatchResumeError("capsule request member ID is invalid") from exc
    raise BatchResumeError("capsule member is outside the closed control-state grammar")


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(type(key) is str for key in value):
        raise BatchResumeError(f"{field_name} must be a string-keyed object")
    return value


def _sequence(value: object, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise BatchResumeError(f"{field_name} must be an array")
    return value


def _closed_mapping(
    value: object, expected: frozenset[str], field_name: str
) -> Mapping[str, object]:
    mapping = _mapping(value, field_name)
    unknown = sorted(set(mapping) - expected)
    missing = sorted(expected - set(mapping))
    if unknown:
        raise BatchResumeError(f"{field_name} has unknown field(s): {', '.join(unknown)}")
    if missing:
        raise BatchResumeError(f"{field_name} lacks required field(s): {', '.join(missing)}")
    return mapping


def _external_requirement_mapping(item: ExternalLocatorRequirement) -> dict[str, object]:
    return {
        "requirement_id": item.requirement_id,
        "owner_validator_id": item.owner_validator_id,
        "role": item.role,
        "original_path": str(item.original_path),
        "expected_sha256": item.expected_sha256,
        "expected_byte_count": item.expected_byte_count,
        "owning_identity_sha256": item.owning_identity_sha256,
        "owning_identity": _mutable_json_tree(item.owning_identity),
    }


def _external_requirement_from_mapping(value: object, *, index: int) -> ExternalLocatorRequirement:
    mapping = _closed_mapping(
        value,
        frozenset(
            {
                "requirement_id",
                "owner_validator_id",
                "role",
                "original_path",
                "expected_sha256",
                "expected_byte_count",
                "owning_identity_sha256",
                "owning_identity",
            }
        ),
        f"external_locator_requirements[{index}]",
    )
    original_path = mapping["original_path"]
    if type(original_path) is not str:
        raise BatchResumeError("external original_path must be text")
    return ExternalLocatorRequirement(
        requirement_id=mapping["requirement_id"],
        owner_validator_id=mapping["owner_validator_id"],
        role=mapping["role"],
        original_path=Path(original_path),
        expected_sha256=mapping["expected_sha256"],
        expected_byte_count=mapping["expected_byte_count"],
        owning_identity_sha256=mapping["owning_identity_sha256"],
        owning_identity=_mapping(mapping["owning_identity"], "owning_identity"),
    )


def _payload_inventory(payloads: Mapping[str, bytes]) -> tuple[ResumeCapsuleMember, ...]:
    return tuple(
        ResumeCapsuleMember(name, _sha256_bytes(payload), len(payload))
        for name, payload in sorted(payloads.items())
    )


def _capsule_root(members: Sequence[ResumeCapsuleMember]) -> str:
    return _sha256_bytes(_canonical_bytes([item.to_mapping() for item in members]))


def _owning_identity(value: Mapping[str, object]) -> tuple[Mapping[str, object], str]:
    normalized = json.loads(_canonical_bytes(dict(value)).decode("utf-8"))
    return normalized, _sha256_bytes(_canonical_bytes(normalized))


def _external_requirements_from_authority(
    original_manifest_bytes: bytes,
    head: BatchStateHead,
    plan_mappings: Sequence[Mapping[str, object]],
    terminal_attempts: Sequence[BatchNodeAttempt],
) -> tuple[ExternalLocatorRequirement, ...]:
    try:
        manifest = BatchManifest.from_json_bytes(original_manifest_bytes)
    except BatchManifestError as exc:
        raise BatchResumeError("retained original manifest is invalid") from exc
    requirements: list[ExternalLocatorRequirement] = []
    for sample in manifest.samples:
        identity, identity_sha256 = _owning_identity(
            {
                "sample_id": sample.sample_id,
                "slide_id": sample.slide_id,
                "source_sha256": head.source_sha256_by_sample[sample.sample_id],
            }
        )
        source_path = sample.source_mif_path.expanduser().resolve(strict=False)
        requirements.append(
            ExternalLocatorRequirement(
                requirement_id=f"source:{sample.sample_id}",
                owner_validator_id="mif-source-image/v1",
                role="source_mif",
                original_path=source_path,
                expected_sha256=head.source_sha256_by_sample[sample.sample_id],
                expected_byte_count=None,
                owning_identity_sha256=identity_sha256,
                owning_identity=identity,
            )
        )
        if sample.reference is not None:
            reference_identity, reference_identity_sha256 = _owning_identity(
                {
                    "sample_id": sample.sample_id,
                    "reference": sample.reference.to_mapping(),
                }
            )
            requirements.append(
                ExternalLocatorRequirement(
                    requirement_id=(
                        f"reference:{sample.sample_id}:{sample.reference.reference_id}"
                    ),
                    owner_validator_id="mif-source-image/v1",
                    role="paired_reference_source",
                    original_path=sample.reference.source_path.expanduser().resolve(strict=False),
                    expected_sha256=None,
                    expected_byte_count=None,
                    owning_identity_sha256=reference_identity_sha256,
                    owning_identity=reference_identity,
                )
            )
    # Source selections remain in retained plans after later replacements. Use
    # their existing descriptors for both current and archived paired images.
    from .batch_project import PairedMovingReferenceAuthority
    selected_pairs = {}
    retained_samples = {sample.sample_id for sample in manifest.samples}
    for plan in plan_mappings:
        for node in plan["nodes"]:
            revision = node.get("settled_review_revision", {})
            for raw_pair in (node.get("paired_reference"), revision.get("paired_reference")):
                if raw_pair is None:
                    continue
                paired = PairedMovingReferenceAuthority.from_mapping(raw_pair)
                participants = node["participant_ids"]
                if len(participants) != 1 or participants[0] not in retained_samples:
                    raise BatchResumeError("Paired image selection has no unique retained sample")
                selected_pairs[(participants[0], paired.descriptor_sha256)] = paired
    for (sample_id, descriptor), paired in sorted(selected_pairs.items()):
        identity, identity_sha256 = _owning_identity({
            "sample_id": sample_id, "reference": paired.to_mapping()})
        requirements.append(ExternalLocatorRequirement(
            requirement_id=f"reference:{sample_id}:{descriptor}",
            owner_validator_id="mif-source-image/v1", role="paired_reference_source",
            original_path=paired.source_path, expected_sha256=paired.source_sha256,
            expected_byte_count=None, owning_identity_sha256=identity_sha256,
            owning_identity=identity))
    designated_project = manifest.project_path
    if designated_project is None and manifest.create_project_path is not None:
        designated_project = manifest.create_project_path
    if designated_project is not None:
        project_path = designated_project.expanduser().resolve(strict=False)
        project_identity, project_identity_sha256 = _owning_identity(
            {
                "project_identity_sha256": head.project_identity_sha256,
            }
        )
        requirements.append(
            ExternalLocatorRequirement(
                requirement_id="project:designated",
                owner_validator_id="qupath-project/v1",
                role="qupath_project",
                original_path=project_path,
                expected_sha256=head.project_qpproj_snapshot_sha256,
                expected_byte_count=None,
                owning_identity_sha256=project_identity_sha256,
                owning_identity=project_identity,
            )
        )
    artifact_mappings: dict[str, Mapping[str, object]] = {}
    for declaration in manifest.reusable_artifacts:
        mapping = declaration.to_mapping()
        artifact_mappings[_sha256_bytes(_canonical_bytes(mapping))] = mapping
    for plan_mapping in plan_mappings:
        for value in _sequence(plan_mapping["external_inputs"], "plan external_inputs"):
            mapping = _mapping(value, "plan external artifact")
            artifact_mappings[_sha256_bytes(_canonical_bytes(mapping))] = mapping
    for attempt in terminal_attempts:
        for artifact in attempt.output_artifacts:
            artifact_mappings[artifact.artifact_ref_sha256] = artifact.to_mapping()
    for artifact_ref_sha256, mapping in sorted(artifact_mappings.items()):
        if (
            mapping.get("artifact_kind") == "project_catalogue_entry"
            and head.project_catalogue_sha256 is not None
        ):
            # The exact local catalogue generation is an authenticated capsule
            # member installed before the state head; it is not an external
            # scientific payload that requires locator rebinding.
            continue
        locator = mapping.get("locator")
        if locator is None:
            continue
        if type(locator) is not str:
            raise BatchResumeError("artifact locator must be text")
        identity, identity_sha256 = _owning_identity(
            {
                "artifact_kind": mapping.get("artifact_kind"),
                "schema_name": mapping.get("schema_name"),
                "schema_version": mapping.get("schema_version"),
                "scope": mapping.get("scope"),
                "owning_identity": mapping.get("owning_identity"),
                "owning_identity_contract_sha256": mapping.get("owning_identity_contract_sha256"),
            }
        )
        path = Path(locator).expanduser().resolve(strict=False)
        expected_sha256 = mapping.get("sha256")
        expected_byte_count = mapping.get("byte_count")
        schema_name = _text(mapping.get("schema_name"), "artifact schema_name")
        raw_schema_version = mapping.get("schema_version")
        if type(raw_schema_version) is int and raw_schema_version > 0:
            schema_version = str(raw_schema_version)
        else:
            schema_version = _text(raw_schema_version, "artifact schema_version")
        requirements.append(
            ExternalLocatorRequirement(
                requirement_id=f"artifact:{artifact_ref_sha256}",
                owner_validator_id=f"{schema_name}/{schema_version}",
                role="scientific_artifact",
                original_path=path,
                expected_sha256=expected_sha256,
                expected_byte_count=expected_byte_count,
                owning_identity_sha256=identity_sha256,
                owning_identity=identity,
            )
        )
    by_id = {item.requirement_id: item for item in requirements}
    if len(by_id) != len(requirements):
        raise BatchResumeError("external locator requirement identity collision")
    return tuple(sorted(by_id.values(), key=lambda item: item.requirement_id))


def _derive_external_requirements_from_store(
    store: BatchStateStore,
    head: BatchStateHead,
) -> tuple[ExternalLocatorRequirement, ...]:
    plan_mappings: list[Mapping[str, object]] = []
    try:
        original_manifest_bytes = store.retained_manifest(head.normalized_manifest_sha256).original_manifest_bytes
        for plan_sha256 in head.plan_history_sha256s:
            payload = (store.layout.plans / f"{plan_sha256}.json").read_bytes()
            decoded = json.loads(payload.decode("utf-8"))
            if _canonical_bytes(decoded) != payload:
                raise BatchResumeError("retained batch plan is not canonical")
            mapping = _mapping(decoded, "retained batch plan")
            if mapping.get("plan_sha256") != plan_sha256:
                raise BatchResumeError("retained batch plan identity disagrees")
            plan_mappings.append(mapping)
        attempts = tuple(
            store.load_attempt(terminal_sha256)
            for terminal_sha256 in head.terminal_attempt_sha256_by_attempt_id.values()
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, BatchStateError) as exc:
        raise BatchResumeError("cannot reconstruct external locator authority") from exc
    return _external_requirements_from_authority(
        original_manifest_bytes,
        head,
        plan_mappings,
        attempts,
    )


def _derive_external_requirements_from_payloads(
    payloads: Mapping[str, bytes],
    head: BatchStateHead,
    limits: ResumeCapsuleLimits,
) -> tuple[ExternalLocatorRequirement, ...]:
    plan_mappings: list[Mapping[str, object]] = []
    for plan_sha256 in head.plan_history_sha256s:
        name = (
            "plan/batch-plan.json"
            if plan_sha256 == head.active_plan_sha256
            else f"plan/history/{plan_sha256}.json"
        )
        payload = payloads.get(name)
        if payload is None:
            raise BatchResumeError("capsule lacks exact retained plan authority")
        mapping = _mapping(_parse_json(payload, name, limits), name)
        if mapping.get("plan_sha256") != plan_sha256:
            raise BatchResumeError("capsule plan path and identity disagree")
        plan_mappings.append(mapping)
    attempts: list[BatchNodeAttempt] = []
    for terminal_sha256 in head.terminal_attempt_sha256_by_attempt_id.values():
        name = f"attempts/terminal-{terminal_sha256}.json"
        payload = payloads.get(name)
        if payload is None:
            raise BatchResumeError("capsule lacks exact terminal attempt authority")
        try:
            attempt = BatchNodeAttempt.from_mapping(_parse_json(payload, name, limits))
        except BatchStateError as exc:
            raise BatchResumeError("capsule terminal attempt is invalid") from exc
        if attempt.terminal_record_sha256 != terminal_sha256:
            raise BatchResumeError("capsule terminal attempt identity disagrees")
        attempts.append(attempt)
    return _external_requirements_from_authority(
        payloads[_manifest_member(head.normalized_manifest_sha256, "original")],
        head,
        plan_mappings,
        attempts,
    )


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(_safe_member_name(name), date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (0o100600 & 0xFFFF) << 16
    info.flag_bits = 0x800
    return info


def _write_immutable_archive(destination: Path, payloads: Mapping[str, bytes]) -> None:
    if not isinstance(destination, Path):
        raise BatchResumeError("capsule destination must be a Path")
    destination = destination.expanduser().resolve(strict=False)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BatchResumeError("cannot create capsule destination directory") from exc
    if destination.exists() or destination.is_symlink():
        raise BatchResumeError("resume capsule destination already exists")
    temporary = destination.parent / f".{destination.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_STORED) as archive:
                for name, payload in sorted(payloads.items()):
                    archive.writestr(_zip_info(name), payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise BatchResumeError("resume capsule destination already exists") from exc
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BatchResumeError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise BatchResumeError("cannot publish immutable resume capsule") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _assert_json_depth(value: object, maximum: int, depth: int = 1) -> None:
    if depth > maximum:
        raise BatchResumeError("capsule JSON exceeds nesting-depth limit")
    if isinstance(value, Mapping):
        for item in value.values():
            _assert_json_depth(item, maximum, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _assert_json_depth(item, maximum, depth + 1)


def _parse_json(payload: bytes, field_name: str, limits: ResumeCapsuleLimits) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise BatchResumeError(f"{field_name} contains duplicate key {key!r}")
            result[key] = item
        return result

    def reject_constant(value: str) -> object:
        raise BatchResumeError(f"{field_name} contains non-finite number {value}")

    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except BatchResumeError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise BatchResumeError(f"{field_name} is malformed UTF-8 JSON") from exc
    _assert_json_depth(decoded, limits.max_json_depth)
    return decoded


def _read_bounded_archive(
    capsule_path: Path, limits: ResumeCapsuleLimits
) -> tuple[dict[str, bytes], str, Path]:
    if not isinstance(capsule_path, Path):
        raise BatchResumeError("capsule_path must be a Path")
    if not isinstance(limits, ResumeCapsuleLimits):
        raise BatchResumeError("capsule inspection requires ResumeCapsuleLimits")
    path = Path(os.path.abspath(capsule_path.expanduser()))
    try:
        named_metadata = path.lstat()
    except OSError as exc:
        raise BatchResumeError("resume capsule is missing") from exc
    if stat.S_ISLNK(named_metadata.st_mode) or not stat.S_ISREG(named_metadata.st_mode):
        raise BatchResumeError("resume capsule must be a regular file")
    maximum_archive_bytes = limits.max_total_bytes + limits.max_members * 1024 + 65_536
    if named_metadata.st_size > maximum_archive_bytes:
        raise BatchResumeError("resume capsule container exceeds size limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as capsule_stream:
            opened_metadata = os.fstat(capsule_stream.fileno())
            if not stat.S_ISREG(opened_metadata.st_mode) or (
                opened_metadata.st_dev,
                opened_metadata.st_ino,
            ) != (named_metadata.st_dev, named_metadata.st_ino):
                raise BatchResumeError("resume capsule path identity changed")
            digest = hashlib.sha256()
            for chunk in iter(lambda: capsule_stream.read(1024 * 1024), b""):
                digest.update(chunk)
            archive_sha256 = digest.hexdigest()
            capsule_stream.seek(0)
            with zipfile.ZipFile(capsule_stream, mode="r") as archive:
                infos = archive.infolist()
                if not infos or len(infos) > limits.max_members:
                    raise BatchResumeError("resume capsule member count is outside limits")
                names: list[str] = []
                total = 0
                for info in infos:
                    name = _allowed_member_name(info.filename)
                    if name != info.filename or info.is_dir():
                        raise BatchResumeError("resume capsule member name is not canonical")
                    names.append(name)
                    unix_mode = (info.external_attr >> 16) & 0xFFFF
                    if unix_mode and not stat.S_ISREG(unix_mode):
                        raise BatchResumeError(
                            "resume capsule links and special members are forbidden"
                        )
                    if info.flag_bits & 0x1:
                        raise BatchResumeError("encrypted resume capsule members are unsupported")
                    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                        raise BatchResumeError("resume capsule compression method is unsupported")
                    if info.file_size < 0 or info.file_size > limits.max_member_bytes:
                        raise BatchResumeError("resume capsule member exceeds size limit")
                    total += info.file_size
                    if total > limits.max_total_bytes:
                        raise BatchResumeError("resume capsule exceeds total size limit")
                    if info.file_size and (
                        info.compress_size == 0
                        or info.file_size > info.compress_size * limits.max_compression_ratio
                    ):
                        raise BatchResumeError(
                            "resume capsule member exceeds compression-ratio limit"
                        )
                if len(set(names)) != len(names) or len({name.casefold() for name in names}) != len(
                    names
                ):
                    raise BatchResumeError("resume capsule has duplicate or case-colliding members")
                payloads: dict[str, bytes] = {}
                for info in infos:
                    chunks: list[bytes] = []
                    count = 0
                    with archive.open(info, mode="r") as member_stream:
                        while True:
                            chunk = member_stream.read(
                                min(
                                    1024 * 1024,
                                    limits.max_member_bytes + 1 - count,
                                )
                            )
                            if not chunk:
                                break
                            count += len(chunk)
                            if count > info.file_size or count > limits.max_member_bytes:
                                raise BatchResumeError(
                                    "resume capsule stream exceeds declared bounds"
                                )
                            chunks.append(chunk)
                    if count != info.file_size:
                        raise BatchResumeError("resume capsule member size is inconsistent")
                    payloads[info.filename] = b"".join(chunks)
            current_metadata = path.lstat()
            if stat.S_ISLNK(current_metadata.st_mode) or (
                current_metadata.st_dev,
                current_metadata.st_ino,
            ) != (opened_metadata.st_dev, opened_metadata.st_ino):
                raise BatchResumeError("resume capsule path changed while reading")
    except BatchResumeError:
        raise
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
        raise BatchResumeError("resume capsule ZIP structure is invalid") from exc
    return payloads, archive_sha256, path.resolve(strict=True)


_PAUSE_MEMBER_PREFIX = "state/quiescent-pauses/"


def _aggregate_review_request_ids(head, snapshot, load_attempt):
    """Identify state-derived cluster reviews using their existing request key.

    This only determines which requests have no ordinary request file. The state
    owner still reopens and validates their full evidence before accepting a head.
    """
    from .batch_phenotyping.review_target import _interaction_request_id

    requests = set()
    for node_id, node in snapshot.node_by_id.items():
        if (node.get("kind") != "cluster_label_review"
                or node_id in head.invalidated_node_ids
                or head.node_status_by_id.get(node_id) is not StageStatus.NEEDS_INTERACTION):
            continue
        attempt_ids = head.attempt_ids_by_node.get(node_id, ())
        if not attempt_ids:
            continue
        digest = head.terminal_attempt_sha256_by_attempt_id.get(attempt_ids[-1])
        if digest is None:
            continue
        attempt = load_attempt(digest)
        result = attempt.stage_result
        if (attempt.terminal_record_sha256 != digest or attempt.node_id != node_id
                or attempt.attempt_id != attempt_ids[-1]
                or result.status is not StageStatus.NEEDS_INTERACTION
                or result.stage != "cluster_label_review"
                or result.resume_context.get("schema_version")
                != "mif-batch-cluster-label-review-interaction/v1"):
            raise BatchResumeError("aggregate review terminal differs from its selected node")
        request_id = _interaction_request_id(
            node_id=node_id, attempt_id=attempt.attempt_id,
            attempt_authorization_sha256=attempt.authorization_sha256,
            interaction_context_sha256=_sha256_bytes(
                _canonical_bytes(result.to_mapping()["resume_context"])),
        )
        if request_id in head.pending_interaction_ids:
            requests.add(request_id)
    return requests


def _capsule_pause_records(payloads, head):
    """Return only the pause selected by this exact source head."""
    from .batch_pause import BatchPauseError, BatchQuiescentPauseRecord, _validate_imported_pause

    selected = head.quiescent_pause_sha256
    expected = set() if selected is None else {f"{_PAUSE_MEMBER_PREFIX}{selected}.json"}
    if {name for name in payloads if name.startswith(_PAUSE_MEMBER_PREFIX)} != expected:
        raise BatchResumeError("capsule pause members do not match the selected source pause")
    if selected is None:
        return {}
    payload = payloads[next(iter(expected))]
    try:
        record = BatchQuiescentPauseRecord.from_mapping(json.loads(payload))
        if _canonical_bytes(record.to_mapping()) != payload:
            raise ValueError("pause bytes are not canonical")
        _validate_imported_pause(record, head)
    except (BatchPauseError, ValueError, TypeError) as exc:
        raise BatchResumeError("capsule pause does not match its source head") from exc
    return {selected: payload}


def _evolve_import_head(head, pauses, **changes):
    """Plan one deterministic operational transition while preserving a pause."""
    candidate = head.evolve(**changes)
    if head.quiescent_pause_sha256 is not None:
        from .batch_pause import BatchQuiescentPauseRecord, _import_pause_successor

        source = BatchQuiescentPauseRecord.from_mapping(
            json.loads(pauses[head.quiescent_pause_sha256]))
        record = _import_pause_successor(source, predecessor=head, successor=candidate)
        pauses[record.sha256] = _canonical_bytes(record.to_mapping())
        candidate = head.evolve(**changes, quiescent_pause_sha256=record.sha256)
    return candidate


def _read_store_payloads(store: BatchStateStore, head: BatchStateHead) -> dict[str, bytes]:
    layout = store.layout
    try:
        payloads = {
            _manifest_member(head.normalized_manifest_sha256, "original"): store.retained_manifest(head.normalized_manifest_sha256).original_manifest_bytes,
            _manifest_member(head.normalized_manifest_sha256, "normalized"): store.retained_manifest(head.normalized_manifest_sha256).normalized_manifest_bytes,
            "plan/batch-plan.json": (layout.plans / f"{head.active_plan_sha256}.json").read_bytes(),
            "state/head.json": _canonical_bytes(head.to_mapping()),
        }
    except OSError as exc:
        raise BatchResumeError("batch state changed or is incomplete during export") from exc
    if head.quiescent_pause_sha256 is not None:
        pause = store.load_quiescent_pause_record(head.quiescent_pause_sha256)
        payloads[f"{_PAUSE_MEMBER_PREFIX}{pause.sha256}.json"] = _canonical_bytes(pause.to_mapping())
    for plan_sha256 in head.plan_history_sha256s[:-1]:
        try:
            payloads[f"plan/history/{plan_sha256}.json"] = (
                layout.plans / f"{plan_sha256}.json"
            ).read_bytes()
        except OSError as exc:
            raise BatchResumeError("batch plan history is incomplete") from exc
    manifest_digests = {head.normalized_manifest_sha256}
    for digest in head.plan_history_sha256s:
        retained_plan = store._load_plan_snapshot(digest)
        manifest_digests.add(retained_plan.normalized_manifest_sha256)
    for digest in manifest_digests:
        retained = store.retained_manifest(digest)
        payloads[_manifest_member(digest, "original")] = retained.original_manifest_bytes
        payloads[_manifest_member(digest, "normalized")] = retained.normalized_manifest_bytes
    authorization_digests = set(head.pending_authorization_sha256_by_node.values())
    response_digests: set[str] = set()
    for attempt_id, terminal_sha256 in head.terminal_attempt_sha256_by_attempt_id.items():
        attempt = store.load_attempt(terminal_sha256)
        authorization_digests.add(attempt.authorization_sha256)
        path = layout.attempts / f"terminal-{terminal_sha256}.json"
        payloads[f"attempts/terminal-{terminal_sha256}.json"] = path.read_bytes()
    for authorization_sha256 in sorted(authorization_digests):
        authorization = store.load_authorization(authorization_sha256)
        if authorization.interaction_response_sha256 is not None:
            response_digests.add(authorization.interaction_response_sha256)
        path = layout.attempts / f"authorization-{authorization_sha256}.json"
        payloads[f"attempts/authorization-{authorization_sha256}.json"] = path.read_bytes()
    for response_sha256 in sorted(response_digests):
        path = layout.interactions / f"response-{response_sha256}.json"
        try:
            payloads[f"interactions/response-{response_sha256}.json"] = path.read_bytes()
        except OSError as exc:
            raise BatchResumeError("accepted interaction response is missing") from exc
    for request_id in head.pending_interaction_ids:
        if store._is_pending_cluster_label_review_request(head, request_id):
            continue  # Its exact target is reconstructed from the retained graph.
        path = layout.interactions / f"request-{request_id}.json"
        try:
            payloads[f"interactions/request-{request_id}.json"] = path.read_bytes()
        except OSError as exc:
            raise BatchResumeError("pending interaction request is missing") from exc
    provider = store._project_mutation_provider
    scope = store.workspace_storage_scope_sha256
    if head.pending_training_fit_set_sha256_by_node and (provider is None or scope is None):
        raise BatchResumeError("selected native fits require their runtime authority owner")
    for checkpoint_sha256 in sorted(set(head.pending_training_fit_set_sha256_by_node.values())):
        try:
            checkpoint = store._load_training_fit_set(checkpoint_sha256)
        except BatchStateError as exc:
            raise BatchResumeError("selected native fit checkpoint is missing") from exc
        payloads[f"state/training-fit-sets/{checkpoint_sha256}.json"] = _canonical_bytes(
            checkpoint.to_mapping()
        )
        for entry in checkpoint.entries:
            try:
                material = provider.load_native_classifier_fit_material(
                    entry.project_identity_sha256,
                    entry.runtime_fit_material_sha256,
                    workspace_storage_scope_sha256=scope,
                )
            except Exception as exc:
                raise BatchResumeError("selected native fit runtime material is missing") from exc
            payloads[
                "project/native-fit-materials/" f"{entry.runtime_fit_material_sha256}.json"
            ] = _canonical_bytes(material.to_mapping())
    return payloads


def _annotation_capsule_records(payloads):
    prefix = "project/annotation-publications/"
    return {name[len(prefix):]: value for name, value in payloads.items() if name.startswith(prefix)}


def _annotation_request_ids(payloads):
    return {json.loads(value)["request_id"] for name, value in _annotation_capsule_records(payloads).items()
            if name.endswith("/ticket.json")}


_PLAN_CONTRACT_PREFIX = "project/plan-runtime-contracts/"


def _required_plan_contracts(plans, manifests, current_digest):
    """Select the current graph and the two sides of each memory-only revision."""
    required = {current_digest}
    ordered = sorted(plans.values(), key=lambda plan: plan["revision"])
    for previous, current in zip(ordered, ordered[1:]):
        before_digest = previous["normalized_manifest_sha256"]
        after_digest = current["normalized_manifest_sha256"]
        if before_digest == after_digest:
            continue
        before = dict(manifests[before_digest])
        after = dict(manifests[after_digest])
        before["execution_config"] = dict(before["execution_config"])
        after["execution_config"] = dict(after["execution_config"])
        old = before["execution_config"].pop("max_working_set_bytes", None)
        new = after["execution_config"].pop("max_working_set_bytes", None)
        if old != new and before == after:
            required.update((previous["plan_sha256"], current["plan_sha256"]))
    return required


def _capsule_plan_inputs(payloads):
    plans = {value["plan_sha256"]: value for name, body in payloads.items()
             if name == "plan/batch-plan.json" or name.startswith("plan/history/")
             for value in [json.loads(body)]}
    manifests = {name.split("/")[1]: json.loads(body) for name, body in payloads.items()
                 if name.startswith("manifest/") and name.endswith("/normalized.json")}
    current = json.loads(payloads["state/head.json"])["active_plan_sha256"]
    return plans, manifests, current


def _parse_plan_contracts(payloads, *, plans=None, manifests=None, current_digest=None):
    """Reopen the existing typed graph records before publishing destination files.

    Capsule inspection requires the exact selected inventory. Import additionally
    retains the authenticated original current graph when its workspace-only
    successor becomes current; every supplied contract must still match a plan.
    """
    from .batch_state import _plan_contract_catalog, _plan_from_material
    capsule = plans is None
    if capsule:
        plans, manifests, current_digest = _capsule_plan_inputs(payloads)
    required = _required_plan_contracts(plans, manifests, current_digest)
    bodies = {name[len(_PLAN_CONTRACT_PREFIX):-5]: body for name, body in payloads.items()
              if name.startswith(_PLAN_CONTRACT_PREFIX)}
    if (capsule and set(bodies) != required) or (not capsule and not required <= set(bodies)):
        raise BatchResumeError("capsule plan contracts do not cover the required saved graphs")
    records = {}
    for digest, body in bodies.items():
        if digest not in plans:
            raise BatchResumeError("capsule plan contract names an unknown graph")
        value = json.loads(body)
        if _canonical_bytes(value) != body:
            raise BatchResumeError("capsule plan contract is not canonical")
        contracts = _plan_contract_catalog(value, digest)
        plan = _plan_from_material(plans[digest], contracts)
        if _canonical_bytes(plan.to_mapping()) != _canonical_bytes(plans[digest]):
            raise BatchResumeError("capsule typed plan differs from its saved graph")
        records[digest] = plan
    return records


def _retained_plan_contracts(store, payloads):
    plans, manifests, current = _capsule_plan_inputs(payloads)
    return {_PLAN_CONTRACT_PREFIX + digest + ".json":
            store._plan_runtime_contract_path(digest).read_bytes()
            for digest in sorted(_required_plan_contracts(plans, manifests, current))}


_SETUP_STORE_PREFIX = "project/training-plan-store/"
_SETUP_STORE_MARKER = ".mif-training-plan-set-store.json"


def _accepted_setup_identities(attempts):
    """Select setup bodies named by completed, retained interaction results."""
    identities = {}
    for attempt in attempts:
        if attempt.stage_result.status is not StageStatus.COMPLETED:
            continue
        for artifact in attempt.output_artifacts:
            if artifact.artifact_kind != "training_plan_set":
                continue
            identity = artifact.owning_identity
            digest = identity["body_sha256"]
            prior = identities.get(digest)
            if prior is not None and prior != identity:
                raise BatchResumeError("accepted setup references disagree")
            identities[digest] = identity
    return identities


def _parse_setup_store(payloads, attempts=None):
    """Authenticate original setup-store bytes without creating a destination store.

    The accepted artifacts supply the marker identity, body hash, byte count and
    relative locator. Only these bodies travel with an analysis; unrelated files
    in its setup store are not imported.
    """
    from .batch_state import BatchNodeAttempt
    from .batch_training.runtime import TrainingPlanSetStore
    from .batch_training import TrainingPlanSet
    from .batch_training.runtime import MAX_TRAINING_PLAN_SET_BYTES
    if attempts is None:
        attempts = (BatchNodeAttempt.from_mapping(json.loads(value))
                    for name, value in payloads.items() if name.startswith("attempts/terminal-"))
    identities = _accepted_setup_identities(attempts)
    files = {name[len(_SETUP_STORE_PREFIX):]: value for name, value in payloads.items()
             if name.startswith(_SETUP_STORE_PREFIX)}
    expected = ({_SETUP_STORE_MARKER} | {f"objects/{digest}.json" for digest in identities}
                if identities else set())
    if set(files) != expected:
        raise BatchResumeError("capsule setup-store inventory differs from accepted setups")
    if not identities:
        return {}
    marker = TrainingPlanSetStore._parse_store_identity(files[_SETUP_STORE_MARKER])
    records = {}
    for digest, identity in identities.items():
        locator = f"objects/{digest}.json"
        body = files[locator]
        if (identity["store_identity_sha256"] != marker
                or identity["store_relative_locator"] != locator
                or len(body) != identity["body_byte_count"]
                or not 0 < len(body) <= MAX_TRAINING_PLAN_SET_BYTES
                or hashlib.sha256(body).hexdigest() != digest):
            raise BatchResumeError("capsule setup body differs from its accepted reference")
        material = TrainingPlanSet.from_mapping(json.loads(body))
        if _canonical_bytes(material.to_mapping()) != body:
            raise BatchResumeError("capsule setup body is not canonical")
        records[digest] = material
    return records


def _retained_setup_payloads(store, head):
    """Copy the existing marker and accepted setup bodies without changing identity."""
    from .batch_training.runtime import TrainingPlanSetStore
    identities = _accepted_setup_identities(
        store.load_attempt(digest) for digest in head.terminal_attempt_sha256_by_attempt_id.values())
    if not identities:
        return {}
    payloads = {}
    with TrainingPlanSetStore(store.layout.state / "training-plan-sets") as retained:
        root, _ = retained._require_authorities()
        marker = root.read_immutable(_SETUP_STORE_MARKER, max_bytes=1024,
                                     label="exported setup-store identity")
        payloads[_SETUP_STORE_PREFIX + _SETUP_STORE_MARKER] = marker
        for digest, identity in identities.items():
            if identity["store_identity_sha256"] != retained.store_identity_sha256:
                raise BatchResumeError("accepted setup belongs to another store")
            material = retained.reopen(identity["store_relative_locator"],
                sha256=digest, byte_count=identity["body_byte_count"])
            payloads[_SETUP_STORE_PREFIX + f"objects/{digest}.json"] = _canonical_bytes(material.to_mapping())
    return payloads


_SECONDARY_DEFINITION_PREFIX = "project/secondary-definitions/"


def _secondary_definition_refs(attempts):
    """Collect only exact accepted control bodies named by retained attempts."""
    from .batch_secondary_definitions import ARTIFACT_KIND, _definition_record_identity

    refs = {}
    for attempt in attempts:
        for ref in attempt.output_artifacts:
            if ref.artifact_kind != ARTIFACT_KIND:
                continue
            digest, _size = _definition_record_identity(ref)
            previous = refs.setdefault(digest, ref)
            if previous.owning_identity != ref.owning_identity or previous.scope != ref.scope:
                raise BatchResumeError("accepted secondary definition references disagree")
    return refs


def _parse_secondary_definition_records(payloads, attempts=None):
    """Decode exact selected bodies before history checks or destination writes."""
    from .batch_secondary_definitions import _decode_definition_material
    from .batch_state import BatchNodeAttempt

    if attempts is None:
        attempts = (BatchNodeAttempt.from_mapping(json.loads(value))
                    for name, value in payloads.items() if name.startswith("attempts/terminal-"))
    refs = _secondary_definition_refs(attempts)
    files = {name[len(_SECONDARY_DEFINITION_PREFIX):]: value for name, value in payloads.items()
             if name.startswith(_SECONDARY_DEFINITION_PREFIX)}
    if set(files) != {f"{digest}.json" for digest in refs}:
        raise BatchResumeError("capsule secondary definitions differ from accepted choices")
    return {digest: _decode_definition_material(ref, record_payload=files[f"{digest}.json"])
            for digest, ref in refs.items()}


def _retained_secondary_definition_payloads(store, head):
    from .batch_secondary_definitions import _read_definition_record

    refs = _secondary_definition_refs(
        store.load_attempt(digest) for digest in head.terminal_attempt_sha256_by_attempt_id.values())
    directory = store.layout.state / "secondary-definitions"
    return {_SECONDARY_DEFINITION_PREFIX + f"{digest}.json": _read_definition_record(ref, directory)[0]
            for digest, ref in refs.items()}


def _retained_saved_state_payloads(store, head, project_authority, catalogue_store):
    """Transport existing accepted observations, without copying candidate hierarchies."""
    from .cluster.cluster_annotations import annotation_publication_records
    payloads = _retained_setup_payloads(store, head)
    payloads.update(_retained_secondary_definition_payloads(store, head))
    for attestation in project_authority.mutation_attestation_history:
        if attestation.mutation_kind == "project_stage":
            digest = attestation.operation_evidence_sha256
            value = store._read_batch_project_ref(store._batch_project_ref(
                "batch_project_runtime_evidence", evidence_sha256=digest),
                label="capsule saved project-stage observation")
            payloads[f"project/stage-evidence/{digest}.json"] = _canonical_bytes(value)
    required_materials = {
        item.operation_evidence_sha256
        for item in project_authority.mutation_attestation_history
        if item.mutation_kind == "annotation_classifier"
    }
    covered_materials: set[str] = set()
    for digest in store._retained_training_fit_set_digests(head):
        checkpoint = store._load_training_fit_set(digest)
        materials = {entry.runtime_fit_material_sha256 for entry in checkpoint.entries}
        if checkpoint.batch_id != head.batch_id or not materials.intersection(required_materials):
            continue
        if (
            digest not in head.pending_training_fit_set_sha256_by_node.values()
            and set(checkpoint.completed_lease_archive_ref_by_fit_material_sha256) != materials
        ):
            # The completed checkpoint supersedes the pre-archive checkpoint.
            # Keep an unfinished checkpoint only when current state selects it.
            continue
        if not materials <= required_materials:
            raise BatchResumeError("retained checkpoint contains an unaccepted native fit")
        payloads[f"state/training-fit-sets/{digest}.json"] = _canonical_bytes(checkpoint.to_mapping())
        for entry in checkpoint.entries:
            material = store._project_mutation_provider.load_native_classifier_fit_material(
                entry.project_identity_sha256,
                entry.runtime_fit_material_sha256,
                workspace_storage_scope_sha256=store.workspace_storage_scope_sha256,
            )
            payloads[f"project/native-fit-materials/{material.fit_material_sha256}.json"] = (
                _canonical_bytes(material.to_mapping())
            )
        covered_materials.update(materials)
    if covered_materials != required_materials:
        raise BatchResumeError("accepted native classifier history lacks retained checkpoints")
    for name, value in annotation_publication_records(store.layout.root).items():
        payloads["project/annotation-publications/" + name] = value
    for request_id in _annotation_request_ids(payloads):
        request = store.load_interaction_request(request_id)
        payloads[f"interactions/request-{request_id}.json"] = _canonical_bytes(request.to_mapping())
    catalogue = project_authority.catalogue
    heads = {item.authority_head_sha256: item for item in project_authority.authority_head_history}
    mutations = {item.mutation_attestation_sha256: item for item in project_authority.mutation_attestation_history}
    cursor = project_authority.observed_head
    while catalogue is not None:
        attestation = mutations.get(cursor.latest_mutation_attestation_sha256)
        if attestation is not None and attestation.mutation_kind == "batch_catalogue_upsert":
            baseline = attestation.workspace_catalogue_sha256
            while catalogue.catalogue_sha256 != baseline:
                if catalogue.predecessor_catalogue_sha256 is None:
                    raise BatchResumeError("capsule catalogue lacks its accepted image-upsert generation")
                catalogue = catalogue_store.load_generation(catalogue.predecessor_catalogue_sha256)
                payloads[f"project/catalogues/{catalogue.catalogue_sha256}.json"] = _canonical_bytes(catalogue.to_mapping())
            break
        if cursor.predecessor_authority_head_sha256 is None:
            raise BatchResumeError("capsule project lacks its catalogue baseline")
        cursor = heads[cursor.predecessor_authority_head_sha256]
    return payloads


def _parse_stage_evidence(payloads):
    """Require exactly the typed observations named by accepted mutations."""
    from mif_pipeline.qupath_runtime.catalogue import ProjectMutationAttestation
    from mif_pipeline.qupath_runtime.project_stage import ProjectStageMutationEvidenceRecord
    mutations = {record.mutation_attestation_sha256: record for name, value in payloads.items()
                 if name.startswith("project/mutation-attestations/")
                 for record in [ProjectMutationAttestation.from_mapping(json.loads(value))]}
    expected = {item.operation_evidence_sha256 for item in mutations.values() if item.mutation_kind == "project_stage"}
    records = {name[len("project/stage-evidence/"):-5]: ProjectStageMutationEvidenceRecord.from_mapping(json.loads(value))
               for name, value in payloads.items() if name.startswith("project/stage-evidence/")}
    if set(records) != expected:
        raise BatchResumeError("capsule project-stage observation inventory differs")
    for item in mutations.values():
        if item.mutation_kind != "project_stage":
            continue
        evidence = records[item.operation_evidence_sha256]
        if (evidence.mutation_evidence_sha256 != item.operation_evidence_sha256
                or evidence.project_identity_sha256 != item.project_identity_sha256
                or evidence.predecessor_project_authority_head_sha256 != item.predecessor_authority_head_sha256
                or evidence.mutation_request_sha256 != item.mutation_request_sha256
                or evidence.mutation_authorization_sha256 != item.mutation_authorization_sha256):
            raise BatchResumeError("capsule project-stage observation differs from its accepted mutation")
    return records


def _capsule_saved_states(payloads, *, batch_head, limits):
    """Project current hierarchy hashes from the authenticated capsule records."""
    from .batch_project import BatchProjectCatalogue
    from .batch_project_saved_state import _project_location_history, project_saved_states
    from .cluster.cluster_annotations import accepted_annotation_transitions
    from mif_pipeline.qupath_runtime.catalogue import ProjectAuthorityHead, ProjectMutationAttestation
    from mif_pipeline.pipeline_core import StageResult

    catalogue = BatchProjectCatalogue.from_mapping(json.loads(payloads["project/catalogue.json"]))
    current = ProjectAuthorityHead.from_mapping(json.loads(payloads["project/authority-head.json"]))
    heads = {item.authority_head_sha256: item for name, value in payloads.items()
             if name.startswith("project/authority-heads/")
             for item in [ProjectAuthorityHead.from_mapping(json.loads(value))]}
    mutations = {item.mutation_attestation_sha256: item for name, value in payloads.items()
                 if name.startswith("project/mutation-attestations/")
                 for item in [ProjectMutationAttestation.from_mapping(json.loads(value))]}
    evidence = _parse_stage_evidence(payloads)
    fit_sets, _ = _parse_native_fit_capsule_graph(payloads, head=batch_head, limits=limits)
    fits = {entry.successor_project_authority_head_sha256: entry for group in fit_sets for entry in group.entries}

    def validate_fit(cursor, attestation, predecessor):
        entry = fits.get(cursor.authority_head_sha256)
        if (entry is None or entry.mutation_attestation_sha256 != attestation.mutation_attestation_sha256
                or entry.predecessor_project_authority_head_sha256 != predecessor
                or entry.qpproj_snapshot_sha256 != cursor.qpproj_snapshot_sha256):
            raise BatchResumeError("capsule saved-state native fit lineage differs")

    heads, relocations, locations = _project_location_history(
        current, load_head=heads.__getitem__)

    def load_catalogue(digest):
        result = BatchProjectCatalogue.from_mapping(json.loads(
            payloads[f"project/catalogues/{digest}.json"]))
        if result.catalogue_sha256 != digest:
            raise BatchResumeError("capsule saved-state catalogue digest differs")
        return result

    transitions = accepted_annotation_transitions(
        Path(batch_head.project_path).parent.parent, batch_id=batch_head.batch_id,
        project_file=batch_head.project_path, entries=catalogue.entries,
        load_interaction_request=lambda request_id: StageResult.from_mapping(
            json.loads(payloads[f"interactions/request-{request_id}.json"])),
        load_attempt=lambda digest: BatchNodeAttempt.from_mapping(
            json.loads(payloads[f"attempts/terminal-{digest}.json"])),
        records=_annotation_capsule_records(payloads),
        project_locations=locations,
    )
    return project_saved_states(
        catalogue=catalogue, current=current, project_identity_sha256=batch_head.project_identity_sha256,
        load_head=heads.__getitem__, load_attestation=mutations.__getitem__,
        load_stage_evidence=evidence.__getitem__, validate_native_fit=validate_fit,
        annotation_transitions=transitions,
        load_catalogue=load_catalogue,
        relocations=relocations,
    )


def _parse_native_fit_capsule_graph(
    payloads: Mapping[str, bytes],
    *,
    head: BatchStateHead,
    limits: ResumeCapsuleLimits,
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    """Parse the exact selected fit sets and their runtime-owned materials."""

    from mif_pipeline.orchestration.batch_training_checkpoint import (
        BatchNativeFitCheckpointSet,
    )
    from mif_pipeline.qupath_runtime.catalogue import ProjectMutationAttestation
    from mif_pipeline.qupath_runtime.native_classifier_fit import (
        QuPathNativeClassifierFitMaterialRecord,
    )

    from .batch_state import BatchAttemptAuthorization

    pending_sets = head.pending_training_fit_set_sha256_by_node
    actual_set_names = {name for name in payloads if name.startswith("state/training-fit-sets/")}
    if not {f"state/training-fit-sets/{digest}.json" for digest in pending_sets.values()} <= actual_set_names:
        raise BatchResumeError("capsule native fit-set inventory lacks pending checkpoints")
    accepted_materials = {
        mutation.operation_evidence_sha256
        for name, value in payloads.items()
        if name.startswith("project/mutation-attestations/")
        for mutation in [ProjectMutationAttestation.from_mapping(_parse_json(value, name, limits))]
        if mutation.mutation_kind == "annotation_classifier"
    }
    sets: list[BatchNativeFitCheckpointSet] = []
    expected_materials: dict[str, object] = {}
    for name in sorted(actual_set_names):
        digest = Path(name).stem
        try:
            record = BatchNativeFitCheckpointSet.from_mapping(_parse_json(payloads[name], name, limits))
            authorization_name = f"attempts/authorization-{record.originating_attempt_authorization_sha256}.json"
            authorization = BatchAttemptAuthorization.from_mapping(
                _parse_json(payloads[authorization_name], authorization_name, limits)
            )
        except Exception as exc:
            raise BatchResumeError("capsule native fit-set or its authorization is invalid") from exc
        if (
            record.checkpoint_set_sha256 != digest
            or record.batch_id != head.batch_id
            or record.node_id != authorization.node_id
            or record.originating_attempt_id != authorization.attempt_id
            or record.originating_attempt_authorization_sha256 != authorization.authorization_sha256
            or record.admission_plan_sha256 != authorization.admission_plan_sha256
            or record.node_material_sha256 != authorization.node_material_sha256
            or record.input_artifact_set_sha256 != authorization.input_artifact_set_sha256
            or (digest in pending_sets.values() and pending_sets.get(record.node_id) != digest)
        ):
            raise BatchResumeError("capsule native fit-set path or attempt lineage differs")
        sets.append(record)
        for entry in record.entries:
            existing = expected_materials.get(entry.runtime_fit_material_sha256)
            if existing is not None and existing != entry:
                raise BatchResumeError("capsule native fit material has conflicting selectors")
            expected_materials[entry.runtime_fit_material_sha256] = entry
    if set(expected_materials) != accepted_materials:
        raise BatchResumeError("capsule native fit checkpoints differ from accepted project mutations")
    expected_material_names = {
        f"project/native-fit-materials/{digest}.json" for digest in expected_materials
    }
    actual_material_names = {
        name for name in payloads if name.startswith("project/native-fit-materials/")
    }
    if actual_material_names != expected_material_names:
        raise BatchResumeError("capsule native fit material inventory is not selected exactly")
    materials: list[QuPathNativeClassifierFitMaterialRecord] = []
    for digest, entry in sorted(expected_materials.items()):
        name = f"project/native-fit-materials/{digest}.json"
        try:
            material = QuPathNativeClassifierFitMaterialRecord.from_mapping(
                _parse_json(payloads[name], name, limits)
            )
        except Exception as exc:
            raise BatchResumeError("capsule native fit runtime material is invalid") from exc
        attestation_name = (
            "project/mutation-attestations/" f"{entry.mutation_attestation_sha256}.json"
        )
        try:
            attestation = ProjectMutationAttestation.from_mapping(
                _parse_json(
                    payloads[attestation_name],
                    attestation_name,
                    limits,
                )
            )
        except Exception as exc:
            raise BatchResumeError("capsule native fit mutation attestation is missing") from exc
        if (
            material.fit_material_sha256 != digest
            or material.project_identity_sha256 != entry.project_identity_sha256
            or material.fit_plan_sha256 != entry.fit_plan_sha256
            or material.fit_execution_sha256 != entry.fit_execution_sha256
            or material.training_material_evidence_sha256 != entry.training_material_evidence_sha256
            or material.classifier_id != entry.classifier_id
            or material.classifier_kind
            != (
                "supervised_phenotype"
                if entry.classifier_kind == "supervised_phenotype"
                else "pixel"
            )
            or material.native_classifier_type != entry.native_classifier_type
            or material.classifier_version != entry.classifier_version
            or material.native_classifier_sha256 != entry.native_classifier_sha256
            or material.native_classifier_byte_count != entry.native_classifier_byte_count
            or material.project_relative_path != entry.project_relative_path
            or material.predecessor_project_authority_head_sha256
            != entry.predecessor_project_authority_head_sha256
            or material.qpproj_snapshot_sha256 != entry.qpproj_snapshot_sha256
            or attestation.mutation_kind != "annotation_classifier"
            or attestation.project_identity_sha256 != entry.project_identity_sha256
            or attestation.predecessor_authority_head_sha256
            != entry.predecessor_project_authority_head_sha256
            or attestation.post_save_qpproj_snapshot_sha256 != entry.qpproj_snapshot_sha256
            or attestation.operation_evidence_sha256 != digest
        ):
            raise BatchResumeError("capsule native fit material graph differs from its selector")
        materials.append(material)
    return tuple(sets), tuple(materials)


def _project_authority_payloads(
    authority: ProjectCapsuleAuthority,
    head: BatchStateHead,
) -> dict[str, bytes]:
    from mif_pipeline.orchestration.batch_project import BatchProjectCatalogue
    from mif_pipeline.qupath_runtime.catalogue import (
        ProjectAuthorityHead,
        ProjectIdentityMaterializationAttestation,
        ProjectIdentityRecord,
        ProjectMutationAttestation,
    )

    if any(
        not hasattr(authority, name)
        for name in (
            "identity",
            "observed_head",
            "authority_head_history",
            "mutation_attestation_history",
            "latest_mutation_attestation",
            "materialization_attestation",
            "catalogue",
            "effective_project_path",
            "_capture_capability",
        )
    ):
        raise BatchResumeError("project capsule provider returned foreign authority")
    if not isinstance(authority.identity, ProjectIdentityRecord) or not isinstance(
        authority.observed_head, ProjectAuthorityHead
    ):
        raise BatchResumeError("project capsule authority records are not typed")
    if (
        authority.identity.project_identity_sha256 != head.project_identity_sha256
        or authority.observed_head.project_identity_sha256 != head.project_identity_sha256
        or authority.observed_head.authority_head_sha256 != head.project_authority_head_sha256
        or authority.observed_head.materialization_state != head.project_materialization_state
        or authority.observed_head.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
    ):
        raise BatchResumeError("captured project authority disagrees with batch head")
    latest = authority.latest_mutation_attestation
    head_history = authority.authority_head_history
    mutation_history = authority.mutation_attestation_history
    if (
        not isinstance(head_history, tuple)
        or not head_history
        or not all(isinstance(item, ProjectAuthorityHead) for item in head_history)
        or head_history[0] != authority.observed_head
        or not isinstance(mutation_history, tuple)
        or not all(isinstance(item, ProjectMutationAttestation) for item in mutation_history)
    ):
        raise BatchResumeError("project authority history is invalid")
    if latest is not None and not isinstance(latest, ProjectMutationAttestation):
        raise BatchResumeError("latest project mutation attestation is invalid")
    if (
        (latest is None) != (authority.observed_head.latest_mutation_attestation_sha256 is None)
        or latest is not None
        and latest.mutation_attestation_sha256
        != authority.observed_head.latest_mutation_attestation_sha256
    ):
        raise BatchResumeError("captured latest project attestation is not exact")
    materialization = authority.materialization_attestation
    if materialization is not None and not isinstance(
        materialization, ProjectIdentityMaterializationAttestation
    ):
        raise BatchResumeError("project materialization attestation is invalid")
    if (
        materialization is not None
        and latest is not None
        and latest.mutation_kind == "identity_materialization"
        and materialization.materialization_attestation_sha256 != latest.operation_evidence_sha256
    ):
        raise BatchResumeError("materialization evidence is not named by mutation")
    if authority.observed_head.materialization_state == "materialized" and materialization is None:
        raise BatchResumeError("materialized project lacks materialization evidence")
    catalogue = authority.catalogue
    if catalogue is not None and not isinstance(catalogue, BatchProjectCatalogue):
        raise BatchResumeError("captured project catalogue is invalid")
    if (catalogue is None) != (head.project_catalogue_sha256 is None) or (
        catalogue is not None and catalogue.catalogue_sha256 != head.project_catalogue_sha256
    ):
        raise BatchResumeError("captured project catalogue disagrees with batch head")
    payloads = {
        "project/identity.json": _canonical_bytes(authority.identity.to_mapping()),
        "project/authority-head.json": _canonical_bytes(authority.observed_head.to_mapping()),
    }
    for item in head_history:
        payloads[f"project/authority-heads/{item.authority_head_sha256}.json"] = _canonical_bytes(
            item.to_mapping()
        )
    for item in mutation_history:
        payloads["project/mutation-attestations/" f"{item.mutation_attestation_sha256}.json"] = (
            _canonical_bytes(item.to_mapping())
        )
    if latest is not None:
        payloads["project/latest-mutation-attestation.json"] = _canonical_bytes(latest.to_mapping())
    if materialization is not None:
        payloads["project/identity-materialization-attestation.json"] = _canonical_bytes(
            materialization.to_mapping()
        )
    if catalogue is not None:
        payloads["project/catalogue.json"] = _canonical_bytes(catalogue.to_mapping())
    _parse_and_validate_project_authority_history(
        payloads,
        limits=DEFAULT_RESUME_CAPSULE_LIMITS,
        identity=authority.identity,
        current_head=authority.observed_head,
        materialization=materialization,
    )
    if any(len(payload) > 64 * 1024 for payload in payloads.values()):
        raise BatchResumeError("project authority capsule member exceeds 64 KiB")
    from .batch_source_revision import source_history_members
    histories = source_history_members(Path(head.project_path)) if head.project_materialization_state == "materialized" else {}
    mutation_requests = {item.mutation_request_sha256 for item in mutation_history if item.mutation_kind == "batch_catalogue_upsert"}
    if any(name.split("/")[2] not in mutation_requests
           for name in histories if name.startswith("project/source-history/")):
        raise BatchResumeError("saved source history is not associated with an admitted project mutation")
    payloads.update(histories)
    return payloads


def _parse_and_validate_project_authority_history(
    payloads: Mapping[str, bytes],
    *,
    limits: ResumeCapsuleLimits,
    identity: object,
    current_head: object,
    materialization: object | None,
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    """Authenticate the complete current-to-root global authority chain."""
    from mif_pipeline.qupath_runtime.catalogue import (
        ProjectAuthorityHead,
        ProjectIdentityMaterializationAttestation,
        ProjectIdentityRecord,
        ProjectMutationAttestation,
    )

    if not isinstance(identity, ProjectIdentityRecord) or not isinstance(
        current_head, ProjectAuthorityHead
    ):
        raise BatchResumeError("project authority history requires typed roots")
    heads: dict[str, ProjectAuthorityHead] = {}
    mutations: dict[str, ProjectMutationAttestation] = {}
    for name, payload in payloads.items():
        if name.startswith("project/authority-heads/"):
            digest = name.removeprefix("project/authority-heads/").removesuffix(".json")
            head = ProjectAuthorityHead.from_mapping(_parse_json(payload, name, limits))
            if head.authority_head_sha256 != digest or digest in heads:
                raise BatchResumeError("project authority-head history is not exact")
            heads[digest] = head
        elif name.startswith("project/mutation-attestations/"):
            digest = name.removeprefix("project/mutation-attestations/").removesuffix(".json")
            mutation = ProjectMutationAttestation.from_mapping(_parse_json(payload, name, limits))
            if mutation.mutation_attestation_sha256 != digest or digest in mutations:
                raise BatchResumeError("project mutation-attestation history is not exact")
            mutations[digest] = mutation
    if heads.get(current_head.authority_head_sha256) != current_head:
        raise BatchResumeError("capsule lacks its exact current authority head history")

    used_heads: list[ProjectAuthorityHead] = []
    used_mutations: list[ProjectMutationAttestation] = []
    found_materialization = None
    cursor = current_head
    while True:
        if cursor in used_heads:
            raise BatchResumeError("project authority-head history cycles")
        if cursor.project_identity_sha256 != identity.project_identity_sha256:
            raise BatchResumeError("project authority-head history changes identity")
        used_heads.append(cursor)
        mutation = None
        if cursor.latest_mutation_attestation_sha256 is not None:
            mutation = mutations.get(cursor.latest_mutation_attestation_sha256)
            if (
                mutation is None
                or mutation.project_identity_sha256 != identity.project_identity_sha256
                or mutation.post_save_qpproj_snapshot_sha256 != cursor.qpproj_snapshot_sha256
            ):
                raise BatchResumeError(
                    "project authority head lacks its exact mutation attestation"
                )
            if mutation not in used_mutations:
                used_mutations.append(mutation)
            if mutation.mutation_kind == "identity_materialization":
                if (
                    not isinstance(
                        materialization,
                        ProjectIdentityMaterializationAttestation,
                    )
                    or mutation.operation_evidence_sha256
                    != materialization.materialization_attestation_sha256
                ):
                    raise BatchResumeError("materialization mutation lacks its exact evidence")
                found_materialization = materialization
        predecessor_digest = cursor.predecessor_authority_head_sha256
        if predecessor_digest is None:
            if mutation is not None and mutation.predecessor_authority_head_sha256 is not None:
                raise BatchResumeError("root authority mutation names a predecessor")
            break
        predecessor = heads.get(predecessor_digest)
        if (
            predecessor is None
            or predecessor.revision + 1 != cursor.revision
            or predecessor.project_identity_sha256 != cursor.project_identity_sha256
        ):
            raise BatchResumeError("project authority predecessor history is incomplete")
        if (
            mutation is not None
            and mutation.predecessor_authority_head_sha256 != predecessor_digest
            and not (
                cursor.latest_mutation_attestation_sha256
                == predecessor.latest_mutation_attestation_sha256
                and cursor.qpproj_snapshot_sha256 == predecessor.qpproj_snapshot_sha256
                and cursor.materialization_state == predecessor.materialization_state
            )
        ):
            raise BatchResumeError("project relocation authority history disagrees")
        cursor = predecessor
    if set(heads) != {item.authority_head_sha256 for item in used_heads} or set(mutations) != {
        item.mutation_attestation_sha256 for item in used_mutations
    }:
        raise BatchResumeError("capsule carries unreachable project authority history")
    if (current_head.materialization_state == "materialized") != (
        found_materialization is not None
    ):
        raise BatchResumeError("project materialization history is incomplete")
    return tuple(used_heads), tuple(used_mutations)


def export_resume_capsule(
    store: BatchStateStore,
    destination: Path,
    limits: ResumeCapsuleLimits = DEFAULT_RESUME_CAPSULE_LIMITS,
    *,
    project_provider: ProjectCapsuleAuthorityProvider,
) -> ResumeCapsuleExport:
    """Publish a deterministic immutable control-plane capsule."""
    if not isinstance(store, BatchStateStore):
        raise BatchResumeError("resume export requires BatchStateStore")
    if not isinstance(limits, ResumeCapsuleLimits):
        raise BatchResumeError("resume export requires ResumeCapsuleLimits")
    try:
        with store.acquire_writer_lease("export-resume") as workspace_lease:
            head = store.load_head()
            payloads = _read_store_payloads(store, head)
            catalogue_store = store.project_catalogue_store(workspace_lease)
            with project_provider.capture(
                head=head,
                catalogue_store=catalogue_store,
                workspace_lease=workspace_lease,
            ) as project_authority:
                payloads.update(_project_authority_payloads(project_authority, head))
                payloads.update(_retained_saved_state_payloads(store, head, project_authority, catalogue_store))
                payloads.update(_retained_plan_contracts(store, payloads))
                _parse_plan_contracts(payloads)
                _parse_setup_store(payloads)
                _parse_secondary_definition_records(payloads)
                _parse_stage_evidence(payloads)
                if project_authority._capture_capability is not project_provider:
                    raise BatchResumeError("project capsule authority is foreign to its provider")
                _parse_native_fit_capsule_graph(
                    payloads,
                    head=head,
                    limits=limits,
                )
                members = _payload_inventory(payloads)
                manifest = BatchResumeCapsuleManifest(
                    schema_name=CAPSULE_SCHEMA_NAME,
                    schema_version=CAPSULE_SCHEMA_VERSION,
                    batch_id=head.batch_id,
                    original_manifest_sha256=head.original_manifest_sha256,
                    normalized_manifest_sha256=head.normalized_manifest_sha256,
                    plan_sha256=head.active_plan_sha256,
                    head_sha256=head.sha256,
                    payload_members=members,
                    external_locator_requirements=(
                        _derive_external_requirements_from_store(store, head)
                    ),
                    capsule_root_sha256=_capsule_root(members),
                )
                all_payloads = {
                    **payloads,
                    "capsule.json": _canonical_bytes(manifest.to_mapping()),
                }
                if len(all_payloads) > limits.max_members:
                    raise BatchResumeError("resume capsule exceeds member-count limit")
                if any(len(payload) > limits.max_member_bytes for payload in all_payloads.values()):
                    raise BatchResumeError("resume capsule member exceeds size limit")
                if sum(len(payload) for payload in all_payloads.values()) > limits.max_total_bytes:
                    raise BatchResumeError("resume capsule exceeds total size limit")
                _write_immutable_archive(destination, all_payloads)
                capsule_path = destination.expanduser().resolve(strict=True)
                capsule_sha256 = _file_sha256(capsule_path)
    except BatchStateError as exc:
        raise BatchResumeError("batch workspace is not quiescent and valid") from exc
    return ResumeCapsuleExport(
        capsule_path=capsule_path,
        capsule_sha256=capsule_sha256,
        manifest=manifest,
        member_names=tuple(sorted(all_payloads)),
    )


def inspect_resume_capsule(
    capsule_path: Path,
    destination_root: Path | None = None,
    limits: ResumeCapsuleLimits = DEFAULT_RESUME_CAPSULE_LIMITS,
) -> ResumeCapsuleImportPlan:
    payloads, archive_sha256, normalized_capsule_path = _read_bounded_archive(capsule_path, limits)
    capsule_payload = payloads.get("capsule.json")
    if capsule_payload is None:
        raise BatchResumeError("resume capsule lacks capsule.json")
    decoded_capsule = _parse_json(capsule_payload, "capsule.json", limits)
    if _canonical_bytes(decoded_capsule) != capsule_payload:
        raise BatchResumeError("capsule.json must use canonical JSON bytes")
    manifest = BatchResumeCapsuleManifest.from_mapping(decoded_capsule)
    expected_names = {"capsule.json", *(item.name for item in manifest.payload_members)}
    if set(payloads) != expected_names:
        raise BatchResumeError("resume capsule members disagree with inventory")
    for member in manifest.payload_members:
        payload = payloads[member.name]
        if len(payload) != member.byte_count or _sha256_bytes(payload) != member.sha256:
            raise BatchResumeError("resume capsule payload digest or size mismatch")
        native_history = member.name.startswith("project/source-history/")
        if native_history and member.name.endswith(".qpdata"):
            continue  # Binary QuPath hierarchy; exact bytes are bound by the inventory above.
        native_json = member.name == "project/metadata.json" or native_history
        if not (member.name.startswith("manifest/") and member.name.endswith("/original.json")):
            decoded = _parse_json(payload, member.name, limits)
            if not native_json and _canonical_bytes(decoded) != payload:
                raise BatchResumeError(
                    f"resume capsule member {member.name!r} is not canonical JSON"
                )
    for required_name in (
        _manifest_member(manifest.normalized_manifest_sha256, "original"),
        _manifest_member(manifest.normalized_manifest_sha256, "normalized"),
        "plan/batch-plan.json",
        "state/head.json",
    ):
        if required_name not in payloads:
            raise BatchResumeError(f"resume capsule lacks required member {required_name}")
    original_bytes = payloads[_manifest_member(manifest.normalized_manifest_sha256, "original")]
    normalized_bytes = payloads[_manifest_member(manifest.normalized_manifest_sha256, "normalized")]
    plan_bytes = payloads["plan/batch-plan.json"]
    head_bytes = payloads["state/head.json"]
    if (
        _sha256_bytes(original_bytes) != manifest.original_manifest_sha256
        or _sha256_bytes(normalized_bytes) != manifest.normalized_manifest_sha256
    ):
        raise BatchResumeError("resume capsule manifest digests disagree")
    try:
        original_manifest = BatchManifest.from_json_bytes(original_bytes)
    except BatchManifestError as exc:
        raise BatchResumeError("resume capsule original manifest is invalid") from exc
    normalized_mapping = _mapping(
        _parse_json(normalized_bytes, _manifest_member(manifest.normalized_manifest_sha256, "normalized"), limits),
        _manifest_member(manifest.normalized_manifest_sha256, "normalized"),
    )
    try:
        _plan_snapshot_from_bytes(plan_bytes)
    except BatchStateError as exc:
        raise BatchResumeError(
            "The saved analysis plan is invalid or uses an older format."
        ) from exc
    plan_mapping = _mapping(
        _parse_json(plan_bytes, "plan/batch-plan.json", limits),
        "plan/batch-plan.json",
    )
    try:
        head = BatchStateHead.from_mapping(_parse_json(head_bytes, "state/head.json", limits))
    except BatchStateError as exc:
        raise BatchResumeError("resume capsule state head is invalid") from exc
    if (
        original_manifest.batch_id != manifest.batch_id
        or original_manifest.to_mapping() != normalized_mapping
        or plan_mapping["batch_id"] != manifest.batch_id
        or plan_mapping["normalized_manifest_sha256"] != manifest.normalized_manifest_sha256
        or plan_mapping["plan_sha256"] != manifest.plan_sha256
        or head.batch_id != manifest.batch_id
        or head.original_manifest_sha256 != manifest.original_manifest_sha256
        or head.normalized_manifest_sha256 != manifest.normalized_manifest_sha256
        or head.active_plan_sha256 != manifest.plan_sha256
        or head.sha256 != manifest.head_sha256
    ):
        raise BatchResumeError("resume capsule manifest, plan, and head disagree")
    _capsule_pause_records(payloads, head)
    from .batch_manifest import revalidate_retained_manifest
    manifest_digests = set()
    for digest in head.plan_history_sha256s:
        name = "plan/batch-plan.json" if digest == head.active_plan_sha256 else f"plan/history/{digest}.json"
        retained_plan = _plan_snapshot_from_bytes(payloads[name])
        manifest_digests.add(retained_plan.normalized_manifest_sha256)
    expected_manifest_names = {_manifest_member(digest, part) for digest in manifest_digests
        for part in ("original", "normalized")}
    if {name for name in payloads if name.startswith("manifest/")} != expected_manifest_names:
        raise BatchResumeError("capsule manifest generations do not match exact plan history")
    for digest in manifest_digests:
        retained = revalidate_retained_manifest(payloads[_manifest_member(digest, "original")],
            payloads[_manifest_member(digest, "normalized")])
        if retained.normalized_manifest_sha256 != digest:
            raise BatchResumeError("capsule manifest generation and content disagree")
    _parse_native_fit_capsule_graph(
        payloads,
        head=head,
        limits=limits,
    )
    _parse_plan_contracts(payloads)
    _parse_setup_store(payloads)
    _parse_secondary_definition_records(payloads)
    _parse_stage_evidence(payloads)
    actual_request_names = {
        name
        for name in payloads
        if name.startswith("interactions/request-") and name.endswith(".json")
    }
    aggregate_ids = _aggregate_review_request_ids(
        head, _plan_snapshot_from_bytes(plan_bytes),
        lambda digest: BatchNodeAttempt.from_mapping(json.loads(
            payloads[f"attempts/terminal-{digest}.json"])),
    )
    expected_request_names = {
        f"interactions/request-{request_id}.json"
        for request_id in ((set(head.pending_interaction_ids) - aggregate_ids)
                           | _annotation_request_ids(payloads))
    }
    if actual_request_names != expected_request_names:
        raise BatchResumeError(
            "resume capsule request members disagree with pending interaction authority"
        )
    derived_requirements = _derive_external_requirements_from_payloads(
        payloads,
        head,
        limits,
    )
    if manifest.external_locator_requirements != derived_requirements:
        raise BatchResumeError("capsule locator inventory disagrees with retained authority")
    original_root = original_manifest.output_root.expanduser().resolve(strict=False)
    workspace_rebinding = None
    if destination_root is not None:
        if not isinstance(destination_root, Path):
            raise BatchResumeError("destination_root must be a Path")
        effective_destination = destination_root.expanduser().resolve(strict=False)
        if effective_destination != original_root:
            workspace_rebinding = BatchWorkspaceRebinding(
                original_output_root=original_root,
                destination_root=effective_destination,
                original_manifest_sha256=manifest.original_manifest_sha256,
                rebinding_sha256=_batch_workspace_rebinding_sha256(
                    original_root,
                    effective_destination,
                    manifest.original_manifest_sha256,
                ),
            )
    required_rebindings = tuple(
        item
        for item in derived_requirements
        if not item.original_path.exists()
        and not (
            item.role == "qupath_project"
            and head.project_materialization_state == "reserved"
            and item.expected_sha256 is None
        )
    )
    sample_statuses: list[ResumeSampleStatus] = []
    completed_node_ids = set(head.completed_node_ids)
    pending_node_ids = set(head.pending_authorization_sha256_by_node)
    failed_node_ids = set(head.failure_codes_by_node)
    for sample in sorted(original_manifest.samples, key=lambda item: item.sample_id):
        scoped_nodes = {
            node_id
            for node_id, scope in head.node_scope_by_id.items()
            if sample.sample_id in scope["sample_ids"]
        }
        sample_statuses.append(
            ResumeSampleStatus(
                sample_id=sample.sample_id,
                last_durable_node_ids=head.last_durable_node_ids_by_sample.get(
                    sample.sample_id, ()
                ),
                pending_interaction_ids=(),
                pending_node_ids=tuple(sorted(scoped_nodes & pending_node_ids)),
                failed_node_ids=tuple(sorted(scoped_nodes & failed_node_ids)),
                completed_node_ids=tuple(sorted(scoped_nodes & completed_node_ids)),
                next_eligible_node_ids=head.next_eligible_node_ids_by_sample.get(
                    sample.sample_id, ()
                ),
            )
        )
    domain_statuses: list[ResumeDomainStatus] = []
    for domain_id, status in sorted(head.domain_task_status_by_id.items()):
        scoped_nodes = {
            node_id
            for node_id, scope in head.node_scope_by_id.items()
            if domain_id == scope["validation_domain_id"]
            or domain_id in scope["analysis_domain_ids"]
        }
        domain_statuses.append(
            ResumeDomainStatus(
                domain_id=domain_id,
                status=status,
                last_durable_node_ids=head.last_durable_node_ids_by_domain.get(domain_id, ()),
                pending_node_ids=tuple(sorted(scoped_nodes & pending_node_ids)),
                failed_node_ids=tuple(sorted(scoped_nodes & failed_node_ids)),
                completed_node_ids=tuple(sorted(scoped_nodes & completed_node_ids)),
                next_eligible_node_ids=head.next_eligible_node_ids_by_domain.get(domain_id, ()),
            )
        )
    return ResumeCapsuleImportPlan(
        capsule_path=normalized_capsule_path,
        capsule_sha256=archive_sha256,
        capsule_root_sha256=manifest.capsule_root_sha256,
        original_manifest_sha256=manifest.original_manifest_sha256,
        normalized_manifest_sha256=manifest.normalized_manifest_sha256,
        plan_sha256=manifest.plan_sha256,
        head_sha256=manifest.head_sha256,
        workspace_rebinding=workspace_rebinding,
        locator_requirements=derived_requirements,
        required_rebindings=required_rebindings,
        sample_statuses=tuple(sample_statuses),
        domain_statuses=tuple(domain_statuses),
        limits=limits,
        member_names=tuple(sorted(payloads)),
    )


def _authenticate_resume_capsule_for_destination(
    plan: ResumeCapsuleImportPlan,
    rebindings: tuple[LocatorRebinding, ...],
    destination_store: BatchStateStore,
    validators: ExternalLocatorValidatorRegistry,
) -> _AuthenticatedResumeCapsuleImport:
    if not isinstance(plan, ResumeCapsuleImportPlan):
        raise BatchResumeError("resume import requires ResumeCapsuleImportPlan")
    if not isinstance(destination_store, BatchStateStore):
        raise BatchResumeError("resume import requires BatchStateStore")
    if not isinstance(validators, ExternalLocatorValidatorRegistry):
        raise BatchResumeError("resume import requires ExternalLocatorValidatorRegistry")
    current = inspect_resume_capsule(
        plan.capsule_path,
        destination_root=destination_store.layout.output_root,
        limits=plan.limits,
    )
    stable_fields = (
        "capsule_path",
        "capsule_sha256",
        "capsule_root_sha256",
        "original_manifest_sha256",
        "normalized_manifest_sha256",
        "plan_sha256",
        "head_sha256",
        "workspace_rebinding",
        "locator_requirements",
        "sample_statuses",
        "domain_statuses",
        "limits",
        "member_names",
    )
    if any(getattr(current, name) != getattr(plan, name) for name in stable_fields):
        raise BatchResumeError("resume capsule changed after inspection")
    payloads, archive_sha256, _ = _read_bounded_archive(plan.capsule_path, plan.limits)
    if archive_sha256 != plan.capsule_sha256:
        raise BatchResumeError("resume capsule archive digest changed")
    manifest = BatchResumeCapsuleManifest.from_mapping(
        _parse_json(
            payloads["capsule.json"],
            "capsule.json",
            plan.limits,
        )
    )
    original_manifest = BatchManifest.from_json_bytes(payloads[_manifest_member(plan.normalized_manifest_sha256, "original")])
    head = BatchStateHead.from_mapping(
        _parse_json(
            payloads["state/head.json"],
            "state/head.json",
            plan.limits,
        )
    )
    from mif_pipeline.orchestration.batch_project import BatchProjectCatalogue
    from mif_pipeline.qupath_runtime.catalogue import (
        ProjectAuthorityHead,
        ProjectIdentityMaterializationAttestation,
        ProjectIdentityRecord,
        ProjectMutationAttestation,
    )

    try:
        project_identity = ProjectIdentityRecord.from_mapping(
            _parse_json(
                payloads["project/identity.json"],
                "project/identity.json",
                plan.limits,
            )
        )
        project_head = ProjectAuthorityHead.from_mapping(
            _parse_json(
                payloads["project/authority-head.json"],
                "project/authority-head.json",
                plan.limits,
            )
        )
    except KeyError as exc:
        raise BatchResumeError("resume capsule lacks project authority graph") from exc
    latest_payload = payloads.get("project/latest-mutation-attestation.json")
    materialization_payload = payloads.get("project/identity-materialization-attestation.json")
    catalogue_payload = payloads.get("project/catalogue.json")
    latest = (
        None
        if latest_payload is None
        else ProjectMutationAttestation.from_mapping(
            _parse_json(
                latest_payload,
                "project/latest-mutation-attestation.json",
                plan.limits,
            )
        )
    )
    materialization = (
        None
        if materialization_payload is None
        else ProjectIdentityMaterializationAttestation.from_mapping(
            _parse_json(
                materialization_payload,
                "project/identity-materialization-attestation.json",
                plan.limits,
            )
        )
    )
    catalogue = (
        None
        if catalogue_payload is None
        else BatchProjectCatalogue.from_mapping(
            _parse_json(
                catalogue_payload,
                "project/catalogue.json",
                plan.limits,
            )
        )
    )
    _parse_and_validate_project_authority_history(
        payloads,
        limits=plan.limits,
        identity=project_identity,
        current_head=project_head,
        materialization=materialization,
    )
    fit_sets, fit_materials = _parse_native_fit_capsule_graph(
        payloads,
        head=head,
        limits=plan.limits,
    )
    expected_fit_scope = batch_project_workspace_storage_scope_sha256(
        batch_id=original_manifest.batch_id,
        original_output_root=(original_manifest.output_root.expanduser().resolve(strict=False)),
    )
    if any(
        material.workspace_storage_scope_sha256 != expected_fit_scope for material in fit_materials
    ):
        raise BatchResumeError(
            "capsule native fit material workspace scope differs from original manifest"
        )
    if (
        project_identity.project_identity_sha256 != head.project_identity_sha256
        or project_head.project_identity_sha256 != head.project_identity_sha256
        or project_head.authority_head_sha256 != head.project_authority_head_sha256
        or project_head.materialization_state != head.project_materialization_state
        or project_head.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
        or (latest is None) != (project_head.latest_mutation_attestation_sha256 is None)
        or latest is not None
        and latest.mutation_attestation_sha256 != project_head.latest_mutation_attestation_sha256
        or (catalogue is None) != (head.project_catalogue_sha256 is None)
        or catalogue is not None
        and catalogue.catalogue_sha256 != head.project_catalogue_sha256
    ):
        raise BatchResumeError("resume project authority graph disagrees with state")
    if project_head.materialization_state == "materialized":
        if (
            latest is None
            or materialization is None
            or materialization.project_identity_sha256 != project_identity.project_identity_sha256
            or materialization.origin_kind != project_identity.origin_kind
            or materialization.origin_qpproj_sha256 != project_identity.origin_qpproj_sha256
        ):
            raise BatchResumeError(
                "materialized project lacks exact identity materialization evidence"
            )
    elif materialization is not None:
        raise BatchResumeError("reserved project cannot carry materialization evidence")
    if catalogue is not None:
        primary = {
            entry.sample_id: entry
            for entry in catalogue.entries
            if entry.image_role == "primary_mif"
        }
        expected_ids = {
            sample_id: value
            for sample_id, value in head.sample_project_entry_id_by_sample.items()
            if value is not None
        }
        expected_projections = {
            sample_id: value
            for sample_id, value in (head.sample_project_entry_projection_sha256_by_sample.items())
            if value is not None
        }
        if (
            catalogue.project_identity_sha256 != project_identity.project_identity_sha256
            or expected_ids
            != {sample_id: entry.project_entry_id for sample_id, entry in primary.items()}
            or expected_projections
            != {sample_id: entry.entry_projection_sha256 for sample_id, entry in primary.items()}
        ):
            raise BatchResumeError("resume catalogue entry and saved-state projections disagree")
    derived_requirements = _derive_external_requirements_from_payloads(
        payloads,
        head,
        plan.limits,
    )
    if (
        manifest.external_locator_requirements != derived_requirements
        or plan.locator_requirements != derived_requirements
    ):
        raise BatchResumeError(
            "resume locator inventory changed or disagrees with retained authority"
        )
    original_root = original_manifest.output_root.expanduser().resolve(strict=False)
    destination_root = destination_store.layout.output_root
    if destination_store.layout.batch_id != manifest.batch_id:
        raise BatchResumeError("destination store has a different batch identity")
    if destination_root == original_root:
        if plan.workspace_rebinding is not None:
            raise BatchResumeError("same-root import cannot carry workspace rebinding")
    else:
        rebinding = plan.workspace_rebinding
        if (
            rebinding is None
            or rebinding.original_output_root != original_root
            or rebinding.destination_root != destination_root
            or rebinding.original_manifest_sha256 != manifest.original_manifest_sha256
            or rebinding.rebinding_sha256
            != _batch_workspace_rebinding_sha256(
                original_root, destination_root, manifest.original_manifest_sha256
            )
        ):
            raise BatchResumeError("destination requires exact workspace rebinding authority")

    requirements = {item.requirement_id: item for item in derived_requirements}
    supplied = tuple(rebindings)
    if not all(isinstance(item, LocatorRebinding) for item in supplied):
        raise BatchResumeError("locator rebindings must be typed")
    supplied_by_id = {item.requirement_id: item for item in supplied}
    if len(supplied_by_id) != len(supplied) or not set(supplied_by_id) <= set(requirements):
        raise BatchResumeError("locator rebindings are duplicate or unknown")
    missing_ids = {
        item.requirement_id
        for item in derived_requirements
        if not item.original_path.exists()
        and not (
            item.role == "qupath_project"
            and head.project_materialization_state == "reserved"
            and item.expected_sha256 is None
        )
    }
    if not missing_ids <= set(supplied_by_id):
        raise BatchResumeError("missing external locators require explicit rebinding")
    receipts: list[ExternalIdentityReceipt] = []
    for requirement_id, requirement in sorted(requirements.items()):
        rebinding = supplied_by_id.get(requirement_id)
        effective_path = (
            (requirement.original_path if rebinding is None else rebinding.replacement_path)
            .expanduser()
            .resolve(strict=False)
        )
        if (
            requirement.role == "qupath_project"
            and head.project_materialization_state == "reserved"
            and requirement.expected_sha256 is None
            and not effective_path.exists()
        ):
            unsigned_receipt = {
                "requirement_id": requirement.requirement_id,
                "owner_validator_id": requirement.owner_validator_id,
                "validated_path": str(effective_path),
                "sha256": None,
                "byte_count": None,
                "owning_identity_sha256": requirement.owning_identity_sha256,
            }
            receipts.append(
                ExternalIdentityReceipt(
                    requirement_id=requirement.requirement_id,
                    owner_validator_id=requirement.owner_validator_id,
                    validated_path=effective_path,
                    sha256=None,
                    byte_count=None,
                    owning_identity_sha256=requirement.owning_identity_sha256,
                    receipt_sha256=_sha256_bytes(_canonical_bytes(unsigned_receipt)),
                )
            )
            continue
        receipt = validators.validate(requirement, effective_path)
        if (
            receipt.requirement_id != requirement.requirement_id
            or receipt.owner_validator_id != requirement.owner_validator_id
            or receipt.validated_path != effective_path
            or receipt.owning_identity_sha256 != requirement.owning_identity_sha256
            or (
                requirement.expected_sha256 is not None
                and receipt.sha256 != requirement.expected_sha256
            )
            or (
                requirement.expected_byte_count is not None
                and receipt.byte_count != requirement.expected_byte_count
            )
        ):
            raise BatchResumeError("owning validation receipt disagrees with requirement")
        receipts.append(receipt)

    plan_bytes_by_sha256: dict[str, bytes] = {}
    active_plan_bytes = payloads["plan/batch-plan.json"]
    active_plan_mapping = _mapping(
        _parse_json(
            active_plan_bytes,
            "plan/batch-plan.json",
            plan.limits,
        ),
        "plan/batch-plan.json",
    )
    plan_bytes_by_sha256[str(active_plan_mapping["plan_sha256"])] = active_plan_bytes
    for name, payload in payloads.items():
        if name.startswith("plan/history/") and name.endswith(".json"):
            digest = name[len("plan/history/") : -len(".json")]
            plan_bytes_by_sha256[_sha256_text(digest, "historical plan digest")] = payload
    pauses = _capsule_pause_records(payloads, head)
    head_changes: dict[str, object] = {}
    if plan.workspace_rebinding is not None:
        successor_unsigned = {
            key: value for key, value in active_plan_mapping.items() if key != "plan_sha256"
        }
        successor_unsigned.update(
            revision=int(active_plan_mapping["revision"]) + 1,
            predecessor_plan_sha256=str(active_plan_mapping["plan_sha256"]),
            workspace_rebinding_sha256=plan.workspace_rebinding.rebinding_sha256,
        )
        successor_sha256 = _sha256_bytes(_canonical_bytes(successor_unsigned))
        successor_bytes = _canonical_bytes({**successor_unsigned, "plan_sha256": successor_sha256})
        plan_bytes_by_sha256[successor_sha256] = successor_bytes
        prior_contract = json.loads(payloads[_PLAN_CONTRACT_PREFIX + head.active_plan_sha256 + ".json"])
        payloads[_PLAN_CONTRACT_PREFIX + successor_sha256 + ".json"] = _canonical_bytes(
            {**prior_contract, "plan_sha256": successor_sha256})
        reasons = dict(head.revision_reason_by_plan_sha256)
        reasons[successor_sha256] = "workspace_rebinding"
        head_changes.update(
            active_plan_sha256=successor_sha256,
            plan_history_sha256s=(*head.plan_history_sha256s, successor_sha256),
            revision_reason_by_plan_sha256=reasons,
        )

    authorization_bytes: dict[str, bytes] = {}
    attempt_bytes: dict[str, bytes] = {}
    interaction_bytes: dict[str, bytes] = {}
    for name, payload in payloads.items():
        if name.startswith("attempts/authorization-") and name.endswith(".json"):
            digest = name[len("attempts/authorization-") : -len(".json")]
            authorization_bytes[_sha256_text(digest, "authorization digest")] = payload
        elif name.startswith("attempts/terminal-") and name.endswith(".json"):
            digest = name[len("attempts/terminal-") : -len(".json")]
            attempt_bytes[_sha256_text(digest, "terminal attempt digest")] = payload
        elif name.startswith("interactions/"):
            interaction_bytes[name[len("interactions/") :]] = payload

    installed_plan_sha256 = str(head_changes.get("active_plan_sha256", head.active_plan_sha256))
    receipt_by_id = {item.requirement_id: item for item in receipts}
    operational_unsigned: dict[str, object] = {
        "schema_name": "mif-batch-operational-rebinding",
        "schema_version": 1,
        "capsule_sha256": plan.capsule_sha256,
        "capsule_root_sha256": plan.capsule_root_sha256,
        "batch_id": manifest.batch_id,
        "original_manifest_sha256": manifest.original_manifest_sha256,
        "normalized_manifest_sha256": manifest.normalized_manifest_sha256,
        "source_head_sha256": head.sha256,
        "installed_plan_sha256": installed_plan_sha256,
        "destination_root": str(destination_root),
        "workspace_rebinding_sha256": (
            None if plan.workspace_rebinding is None else plan.workspace_rebinding.rebinding_sha256
        ),
        "effective_locators": [
            {
                "requirement": _external_requirement_mapping(requirement),
                "effective_path": str(receipt_by_id[requirement.requirement_id].validated_path),
                "validation_receipt": receipt_by_id[requirement.requirement_id].to_mapping(),
            }
            for requirement in derived_requirements
        ],
    }
    operational_rebinding_sha256 = _sha256_bytes(_canonical_bytes(operational_unsigned))
    operational_mapping = {
        **operational_unsigned,
        "operational_rebinding_sha256": operational_rebinding_sha256,
    }
    head_changes["operational_rebinding_sha256"] = operational_rebinding_sha256
    head = _evolve_import_head(head, pauses, **head_changes)
    head_bytes = _canonical_bytes(head.to_mapping())
    operational_rebinding_bytes = _canonical_bytes(operational_mapping)
    plans = tuple(sorted(plan_bytes_by_sha256.items()))
    authorizations = tuple(sorted(authorization_bytes.items()))
    attempts = tuple(sorted(attempt_bytes.items()))
    interactions = tuple(sorted(interaction_bytes.items()))
    receipt_sha256s = tuple(
        sorted(_sha256_bytes(_canonical_bytes(receipt.to_mapping())) for receipt in receipts)
    )
    authority = object.__new__(_AuthenticatedResumeCapsuleImport)
    object.__setattr__(authority, "capsule_sha256", plan.capsule_sha256)
    object.__setattr__(authority, "capsule_root_sha256", plan.capsule_root_sha256)
    object.__setattr__(authority, "destination_root", destination_root)
    object.__setattr__(authority, "layout_root", destination_store.layout.root)
    object.__setattr__(authority, "head_sha256", head.sha256)
    object.__setattr__(authority, "validated_locator_receipt_sha256s", receipt_sha256s)
    object.__setattr__(
        authority,
        "workspace_rebinding_sha256",
        None if plan.workspace_rebinding is None else plan.workspace_rebinding.rebinding_sha256,
    )
    object.__setattr__(
        authority,
        "original_manifest_bytes",
        payloads[_manifest_member(plan.normalized_manifest_sha256, "original")],
    )
    object.__setattr__(
        authority,
        "normalized_manifest_bytes",
        payloads[_manifest_member(plan.normalized_manifest_sha256, "normalized")],
    )
    generations = sorted({str(json.loads(payload)["normalized_manifest_sha256"]) for _, payload in plans})
    manifest_bytes = tuple((digest, payloads[_manifest_member(digest, "original")],
        payloads[_manifest_member(digest, "normalized")]) for digest in generations)
    object.__setattr__(authority, "manifest_bytes_by_sha256", manifest_bytes)
    object.__setattr__(authority, "plan_bytes_by_sha256", plans)
    object.__setattr__(authority, "authorization_bytes_by_sha256", authorizations)
    object.__setattr__(authority, "attempt_bytes_by_sha256", attempts)
    object.__setattr__(authority, "interaction_bytes_by_name", interactions)
    object.__setattr__(authority, "pause_record_bytes_by_sha256", tuple(sorted(pauses.items())))
    object.__setattr__(authority, "_pause_quiescence_graph_sha256", None)
    object.__setattr__(
        authority,
        "training_fit_set_bytes_by_sha256",
        tuple(
            sorted(
                (
                    record.checkpoint_set_sha256,
                    payloads["state/training-fit-sets/" f"{record.checkpoint_set_sha256}.json"],
                )
                for record in fit_sets
            )
        ),
    )
    object.__setattr__(
        authority,
        "native_fit_material_bytes_by_sha256",
        tuple(
            sorted(
                (
                    record.fit_material_sha256,
                    payloads["project/native-fit-materials/" f"{record.fit_material_sha256}.json"],
                )
                for record in fit_materials
            )
        ),
    )
    object.__setattr__(authority, "head_bytes", head_bytes)
    object.__setattr__(
        authority,
        "project_bytes_by_name",
        tuple(
            sorted(
                (name, payload)
                for name, payload in payloads.items()
                if name.startswith("project/")
                and not name.startswith("project/native-fit-materials/")
            )
        ),
    )
    object.__setattr__(
        authority,
        "operational_rebinding_bytes",
        operational_rebinding_bytes,
    )
    object.__setattr__(
        authority,
        "graph_sha256",
        _resume_import_graph_sha256(
            original_manifest_bytes=payloads[_manifest_member(plan.normalized_manifest_sha256, "original")],
            normalized_manifest_bytes=payloads[_manifest_member(plan.normalized_manifest_sha256, "normalized")],
            manifest_bytes_by_sha256=manifest_bytes,
            plan_bytes_by_sha256=plans,
            authorization_bytes_by_sha256=authorizations,
            attempt_bytes_by_sha256=attempts,
            interaction_bytes_by_name=interactions,
            head_bytes=head_bytes,
            operational_rebinding_bytes=operational_rebinding_bytes,
            project_bytes_by_name=authority.project_bytes_by_name,
            training_fit_set_bytes_by_sha256=(authority.training_fit_set_bytes_by_sha256),
            native_fit_material_bytes_by_sha256=(authority.native_fit_material_bytes_by_sha256),
            pause_record_bytes_by_sha256=authority.pause_record_bytes_by_sha256,
        ),
    )
    object.__setattr__(
        authority,
        "_issuer",
        destination_store._resume_capsule_import_capability,
    )
    object.__setattr__(authority, "_consumed", False)
    object.__setattr__(authority, "_recovery_allowed", False)
    return authority


def _bind_authenticated_graph_to_project_destination(
    authority: _AuthenticatedResumeCapsuleImport,
    *,
    effective_project_path: Path,
    destination_authority_head_sha256: str,
) -> _AuthenticatedResumeCapsuleImport:
    """Bind an authenticated capsule graph to the provider-fenced destination.

    This is called only while the capsule import provider retains the workspace,
    path-bootstrap, and identity locks.  It preserves every imported scientific
    reference while replacing the operational project locator and global-head
    admission fence with the destination values established by that provider.
    """
    if not isinstance(authority, _AuthenticatedResumeCapsuleImport):
        raise BatchResumeError("project destination requires authenticated graph")
    try:
        mapping = json.loads(authority.head_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchResumeError("authenticated state head is invalid") from exc
    head = BatchStateHead.from_mapping(mapping)
    effective = effective_project_path.expanduser().resolve(strict=False)
    digest = _sha256_text(
        destination_authority_head_sha256,
        "destination project authority head",
    )
    project_bytes = dict(authority.project_bytes_by_name)
    catalogue_bytes = project_bytes.get("project/catalogue.json")
    catalogue_sha256 = head.project_catalogue_sha256
    if catalogue_bytes is not None:
        from mif_pipeline.orchestration.batch_project import BatchProjectCatalogue

        catalogue = BatchProjectCatalogue.from_mapping(json.loads(catalogue_bytes.decode("utf-8")))
        effective_uri = effective.as_uri()
        if catalogue.project_uri != effective_uri:
            project_bytes[f"project/catalogues/{catalogue.catalogue_sha256}.json"] = catalogue_bytes
            catalogue = BatchProjectCatalogue.create(
                batch_id=catalogue.batch_id,
                project_identity_sha256=catalogue.project_identity_sha256,
                project_uri=effective_uri,
                project_authority_predecessor_head_sha256=(
                    catalogue.project_authority_predecessor_head_sha256
                ),
                qpproj_snapshot_sha256=catalogue.qpproj_snapshot_sha256,
                predecessor_catalogue_sha256=catalogue.catalogue_sha256,
                mutation_request_sha256=catalogue.mutation_request_sha256,
                mutation_authorization_sha256=(catalogue.mutation_authorization_sha256),
                entries=catalogue.entries,
            )
            catalogue_bytes = _canonical_bytes(catalogue.to_mapping())
            project_bytes["project/catalogue.json"] = catalogue_bytes
            catalogue_sha256 = catalogue.catalogue_sha256
            object.__setattr__(
                authority, "project_bytes_by_name", tuple(sorted(project_bytes.items()))
            )
    if (
        head.project_path != str(effective)
        or head.project_authority_head_sha256 != digest
        or head.project_catalogue_sha256 != catalogue_sha256
    ):
        pauses = dict(authority.pause_record_bytes_by_sha256)
        head = _evolve_import_head(
            head, pauses,
            project_path=str(effective),
            project_authority_head_sha256=digest,
            project_catalogue_sha256=catalogue_sha256,
        )
        object.__setattr__(authority, "pause_record_bytes_by_sha256", tuple(sorted(pauses.items())))
    head_bytes = _canonical_bytes(head.to_mapping())
    object.__setattr__(authority, "head_sha256", head.sha256)
    object.__setattr__(authority, "head_bytes", head_bytes)
    object.__setattr__(
        authority,
        "graph_sha256",
        _resume_import_graph_sha256(
            original_manifest_bytes=authority.original_manifest_bytes,
            normalized_manifest_bytes=authority.normalized_manifest_bytes,
            manifest_bytes_by_sha256=authority.manifest_bytes_by_sha256,
            plan_bytes_by_sha256=authority.plan_bytes_by_sha256,
            authorization_bytes_by_sha256=authority.authorization_bytes_by_sha256,
            attempt_bytes_by_sha256=authority.attempt_bytes_by_sha256,
            interaction_bytes_by_name=authority.interaction_bytes_by_name,
            head_bytes=head_bytes,
            operational_rebinding_bytes=authority.operational_rebinding_bytes,
            project_bytes_by_name=authority.project_bytes_by_name,
            training_fit_set_bytes_by_sha256=(authority.training_fit_set_bytes_by_sha256),
            native_fit_material_bytes_by_sha256=(authority.native_fit_material_bytes_by_sha256),
            pause_record_bytes_by_sha256=authority.pause_record_bytes_by_sha256,
        ),
    )
    return authority


class _ProjectImportOrchestrationBridge:
    """Narrow dependency-inversion surface consumed by the runtime provider."""

    BatchResumeError = BatchResumeError
    BatchStateHead = BatchStateHead
    ProjectCapsuleImportSession = ProjectCapsuleImportSession
    ProjectDesignationRequest = ProjectDesignationRequest

    @staticmethod
    def parse_project_catalogue(value: object) -> object:
        from mif_pipeline.orchestration.batch_project import BatchProjectCatalogue

        return BatchProjectCatalogue.from_mapping(value)

    @staticmethod
    def parse_project_authority_history(
        payloads: Mapping[str, bytes],
        *,
        limits: ResumeCapsuleLimits,
        identity: object,
        current_head: object,
        materialization: object | None,
    ) -> tuple[tuple[object, ...], tuple[object, ...]]:
        return _parse_and_validate_project_authority_history(
            payloads,
            limits=limits,
            identity=identity,
            current_head=current_head,
            materialization=materialization,
        )

    @staticmethod
    def saved_states(payloads, *, batch_head, limits):
        return _capsule_saved_states(payloads, batch_head=batch_head, limits=limits)

    @staticmethod
    def authenticate(*args: object) -> _AuthenticatedResumeCapsuleImport:
        return _authenticate_resume_capsule_for_destination(*args)  # type: ignore[arg-type]

    @staticmethod
    def bind_destination(*args: object, **kwargs: object) -> _AuthenticatedResumeCapsuleImport:
        return _bind_authenticated_graph_to_project_destination(*args, **kwargs)  # type: ignore[arg-type]

    @staticmethod
    def resume_catalogue_store(
        store: BatchStateStore,
        authority: _AuthenticatedResumeCapsuleImport,
        workspace_lease: object,
    ) -> object:
        return store._resume_capsule_catalogue_store(  # type: ignore[arg-type]
            authority, workspace_lease
        )

    @staticmethod
    def parse_json(*args: object) -> object:
        return _parse_json(*args)  # type: ignore[arg-type]

    @staticmethod
    def read_archive(
        *args: object,
    ) -> tuple[dict[str, bytes], str, tuple[ResumeCapsuleMember, ...]]:
        return _read_bounded_archive(*args)  # type: ignore[arg-type]


_PROJECT_IMPORT_BRIDGE = _ProjectImportOrchestrationBridge()


def import_resume_capsule(
    plan: ResumeCapsuleImportPlan,
    rebindings: tuple[LocatorRebinding, ...],
    destination_store: BatchStateStore,
    validators: ExternalLocatorValidatorRegistry,
    *,
    project_provider: ProjectCapsuleImportAuthorityProvider,
) -> BatchStateHead:
    """Install one provider-authenticated graph while every authority is live."""
    if not isinstance(destination_store, BatchStateStore):
        raise BatchResumeError("resume import requires BatchStateStore")
    try:
        with destination_store.acquire_writer_lease("import-resume") as workspace_lease:
            with project_provider.open_import_session(
                plan=plan,
                rebindings=rebindings,
                validators=validators,
                destination_store=destination_store,
                workspace_lease=workspace_lease,
                orchestration_bridge=_PROJECT_IMPORT_BRIDGE,
            ) as session:
                if not isinstance(session, ProjectCapsuleImportSession):
                    raise BatchResumeError("project import provider returned a foreign session")
                authority = session._authenticated_graph_capability
                if (
                    not session.is_live
                    or not isinstance(authority, _AuthenticatedResumeCapsuleImport)
                    or authority.graph_sha256 != session.authenticated_graph_sha256
                ):
                    raise BatchResumeError("project import session is stale or changed")
                head = destination_store._install_resume_capsule_graph(
                    authority,
                    workspace_lease,
                    catalogue_import_authority=(session._catalogue_import_authority),
                )
                session.prepare_completion()
                receipt = destination_store.issue_capsule_import_durability_receipt(
                    project_lease=session._session_capability,
                    authenticated_graph_sha256=authority.graph_sha256,
                    lease=workspace_lease,
                )
                session.complete(receipt)
                return head
    except BatchResumeError:
        raise
    except BatchStateError as exc:
        raise BatchResumeError("resume capsule state installation failed") from exc


__all__ = [
    "DEFAULT_RESUME_CAPSULE_LIMITS",
    "BatchResumeCapsuleManifest",
    "BatchResumeError",
    "BatchWorkspaceRebinding",
    "ExternalIdentityReceipt",
    "ExternalLocatorRequirement",
    "ExternalLocatorValidatorRegistry",
    "LocatorRebinding",
    "ProjectCapsuleAuthority",
    "ProjectCapsuleAuthorityProvider",
    "ProjectCapsuleImportAuthorityProvider",
    "ProjectCapsuleImportSession",
    "ResumeCapsuleExport",
    "ResumeCapsuleImportPlan",
    "ResumeCapsuleLimits",
    "ResumeDomainStatus",
    "ResumeSampleStatus",
    "export_resume_capsule",
    "import_resume_capsule",
    "inspect_resume_capsule",
]
