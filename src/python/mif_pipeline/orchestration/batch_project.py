"""Batch-owned composition for one designated QuPath project.

The runtime package owns generic project evidence and mutation authority. This module owns
only sample/slide/domain roles, pairing, deterministic display names, and the
workspace-local immutable catalogue lineage.

The `BatchRegistrationProjectRequest` record carries `schema_name`, `schema_version`,
`batch_id`, `node_id`, `attempt_id` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import hashlib
import json
import math
from pathlib import Path
import re
from threading import RLock
from types import MappingProxyType
from typing import Callable, ContextManager, Literal, Mapping, TypeAlias
from urllib.parse import unquote, urlparse

from mif_pipeline.pipeline_core.authority_layout import (
    AuthorityMemberRefV2,
    AuthorityMemberRetirementV2,
    authority_member_ref_v2,
)
from mif_pipeline.pipeline_core.authority_provisioning import RetainedAuthorityLayoutV2
from mif_pipeline.pipeline_core.durable_records import (
    DurableRecordError,
    canonical_json_bytes,
    read_canonical_record,
)
from mif_pipeline.qupath_runtime.catalogue import (
    ProjectAuthorityError,
    ProjectDesignation,
    ProjectIdentityMaterializationAttestation,
    ProjectMutationArtifactTarget,
    ProjectMutationArtifactInventory,
    ProjectMutationAcquisitionSession,
    ProjectMutationAttestation,
    ProjectMutationLease,
    ProjectMutationLeaseProvider,
    ProjectMutationRecoveryLease,
    ProjectMutationRequestCapability,
    QuPathCatalogueEvidenceEnvelope,
    QuPathCatalogueRequestEnvelope,
    QuPathEntryUpsertRequest,
    QuPathIdentityInstallRequest,
    QuPathProjectOperations,
    QuPathRunnerProjectOperations,
    QuPathProjectSnapshot,
    canonical_project_uri,
)
from mif_pipeline.qupath_runtime.catalogue_operations import (
    QuPathCatalogueOperationError,
)
from mif_pipeline.qupath_runtime.project_content import project_content_matches, same_project_snapshot
from mif_pipeline.qupath_runtime.training import (
    QuPathNativeTrainingRequest,
    decode_qupath_native_training_request,
)
from mif_pipeline.qupath_runtime.project_pyramid import (
    PROJECT_PYRAMID_PARSER_PROFILE_ID,
    ProjectEntryPyramidEvidence,
    ProjectEntryPyramidRequest,
    ProjectPyramidContractError,
    ProjectPyramidScientificIncompatibility,
    parse_project_entry_pyramid_payload,
    retained_project_file,
)
from mif_pipeline.qupath_runtime._script_asset_registry import QuPathScriptAsset
from mif_pipeline.pipeline_core import StageResult, StageStatus
from mif_pipeline.pipeline_core.runtime_lifecycle import (
    RuntimeCompositionBinding,
    RuntimeLifecycleBorrower,
    RuntimeLifecycleError,
)

from .batch_artifacts import (
    ArtifactIdentityContract,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
    validate_artifact_ref,
)
from .batch_analysis_preflight import validate_analysis_frame_artifact_ref
from .batch_state import (
    BatchMaterialAuthorityUpdate,
    BatchRawNodeOutcome,
    BatchStateHead,
    BatchStateStore,
    BatchTerminalCommitter,
    ScheduledNodeResult,
)

JsonValue: TypeAlias = (
    None | bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CATALOGUE_RECORD_MAX_BYTES = 8 * 1024 * 1024
_PROJECT_REQUEST_MAX_BYTES = 256 * 1024
_OPERATION_REPLAY_PLAN_MAX_BYTES = 256 * 1024
_OPERATION_CHECKPOINT_MAX_BYTES = 128 * 1024
_RUNTIME_REQUEST_MAX_BYTES = 256 * 1024
_RUNTIME_EVIDENCE_MAX_BYTES = 8 * 1024 * 1024
_RUNTIME_REPLAY_MAX_BYTES = 8 * 1024 * 1024
PROJECT_UPSERT_SCRIPT_ASSET = QuPathScriptAsset.UPSERT_BATCH_PROJECT_ENTRY
PROJECT_CATALOGUE_ENTRY_IDENTITY_FIELDS: tuple[str, ...] = (
    "project_entry_id",
    "project_identity_sha256",
    "entry_projection_sha256",
    "canonical_image_sha256",
    "saved_state_attestation_sha256",
    "catalogue_sha256",
)
_MIF_PIPELINE_BATCH_RUNTIME_VERSION = "0.1.0"
_PROJECT_CATALOGUE_ENTRY_PRODUCER_VERSION = "mif-pipeline-batch-project/v1"


# ==============================================================================
#                                 Batch project
# ==============================================================================


class BatchProjectError(ValueError):
    """A batch-owned project request or catalogue is invalid."""


class BatchProjectCatalogueConflictError(BatchProjectError):
    """The workspace-local catalogue predecessor changed."""


def project_catalogue_entry_identity_contract() -> ArtifactIdentityContract:
    """Return the public owner contract for one catalogue entry projection."""

    return ArtifactIdentityContract(
        contract_id="mif-batch-project-catalogue-entry-identity/v1",
        artifact_kind="project_catalogue_entry",
        schema_name="mif-batch-project-catalogue",
        schema_version=1,
        required_fields=PROJECT_CATALOGUE_ENTRY_IDENTITY_FIELDS,
        optional_fields=(),
        required_runtime_names=("mif_pipeline",),
    )


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise BatchProjectError("value is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha(value: object, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise BatchProjectError(f"{field} must be a lowercase SHA-256")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BatchProjectError(f"{field} must be non-empty canonical text")
    return value


def _closed(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        raise BatchProjectError(f"{field} is not the current closed schema")
    return value


def _freeze_json(value: object, field: str = "metadata") -> JsonValue:
    if value is None or type(value) in {bool, str}:
        return value  # type: ignore[return-value]
    if type(value) is int:
        return value  # type: ignore[return-value]
    if type(value) is float:
        if not math.isfinite(value):
            raise BatchProjectError(f"{field} contains non-finite number")
        return value
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, field) for item in value)
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        return MappingProxyType({key: _freeze_json(value[key], field) for key in sorted(value)})
    raise BatchProjectError(f"{field} is not canonical JSON")


def _export_json(value: JsonValue) -> object:
    if isinstance(value, tuple):
        return [_export_json(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _export_json(item) for key, item in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class NoMovingReferenceAuthority:
    kind: Literal["none"]
    descriptor_sha256: str

    def __post_init__(self) -> None:
        if self.kind != "none" or _sha(self.descriptor_sha256, "descriptor_sha256") != _digest(
            {"kind": "none"}
        ):
            raise BatchProjectError("no-reference descriptor is invalid")

    @classmethod
    def create(cls) -> "NoMovingReferenceAuthority":
        return cls("none", _digest({"kind": "none"}))

    def to_mapping(self) -> dict[str, object]:
        return {"kind": self.kind, "descriptor_sha256": self.descriptor_sha256}

    @classmethod
    def from_mapping(cls, value: object) -> "NoMovingReferenceAuthority":
        return cls(**dict(_closed(value, frozenset({"kind", "descriptor_sha256"}), "no-reference authority")))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class PairedMovingReferenceAuthority:
    kind: Literal["paired_reference"]
    reference_id: str
    source_path: Path
    canonical_uri: str
    source_sha256: str
    modality: str
    source_image_id: str | None
    source_metadata: Mapping[str, JsonValue]
    descriptor_sha256: str

    def __post_init__(self) -> None:
        if self.kind != "paired_reference":
            raise BatchProjectError("moving-reference kind is invalid")
        _text(self.reference_id, "reference_id")
        if not isinstance(self.source_path, Path) or not self.source_path.is_absolute():
            raise BatchProjectError("reference source_path must be absolute")
        _text(self.canonical_uri, "canonical_uri")
        _sha(self.source_sha256, "source_sha256")
        _text(self.modality, "modality")
        if self.source_image_id is not None:
            _text(self.source_image_id, "source_image_id")
        frozen = _freeze_json(self.source_metadata, "source_metadata")
        if not isinstance(frozen, Mapping):
            raise BatchProjectError("source_metadata must be an object")
        object.__setattr__(self, "source_metadata", frozen)
        if _sha(self.descriptor_sha256, "descriptor_sha256") != _digest(self._unsigned_mapping()):
            raise BatchProjectError("moving-reference descriptor checksum is not canonical")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "reference_id": self.reference_id,
            "source_path": self.source_path.as_posix(),
            "canonical_uri": self.canonical_uri,
            "source_sha256": self.source_sha256,
            "modality": self.modality,
            "source_image_id": self.source_image_id,
            "source_metadata": _export_json(self.source_metadata),
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "descriptor_sha256": self.descriptor_sha256}

    @classmethod
    def create(cls, **values: object) -> "PairedMovingReferenceAuthority":
        unsigned = {"kind": "paired_reference", **values}
        material = dict(unsigned)
        material["source_path"] = Path(material["source_path"]).as_posix()  # type: ignore[arg-type]
        material["source_metadata"] = _export_json(_freeze_json(material["source_metadata"]))
        return cls(**unsigned, descriptor_sha256=_digest(material))  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "PairedMovingReferenceAuthority":
        fields = frozenset(
            {
                "kind",
                "reference_id",
                "source_path",
                "canonical_uri",
                "source_sha256",
                "modality",
                "source_image_id",
                "source_metadata",
                "descriptor_sha256",
            }
        )
        material = dict(_closed(value, fields, "moving-reference authority"))
        material["source_path"] = Path(material["source_path"])
        return cls(**material)  # type: ignore[arg-type]


MovingReferenceAuthority = NoMovingReferenceAuthority | PairedMovingReferenceAuthority


@dataclass(frozen=True, slots=True)
class ProjectDesignationRequest:
    schema_name: Literal["mif-qupath-project-designation-request"]
    schema_version: Literal[1]
    batch_id: str
    intent: Literal["create", "open_existing", "relocate", "cloned_as_new"]
    project_uri: str
    authorizing_record_sha256: str
    expected_project_identity_sha256: str | None
    expected_authority_head_sha256: str | None
    relocation_from_uri: str | None
    clone_source_project_uri: str | None
    clone_source_project_identity_sha256: str | None
    clone_source_qpproj_snapshot_sha256: str | None
    clone_source_identity_sidecar_sha256: str | None
    clone_source_entry_projection_set_sha256: str | None
    clone_required_saved_state_set_sha256: str | None
    request_sha256: str

    def __post_init__(self) -> None:
        """Validate the ProjectDesignationRequest fields when the record is constructed.

        The checks cover ``batch_id``, ``intent``, ``project_uri``,
        ``authorizing_record_sha256``, ``relocation_from_uri``,
        ``clone_source_project_uri``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if self.schema_name != "mif-qupath-project-designation-request" or self.schema_version != 1:
            raise BatchProjectError("designation request schema is unsupported")
        _text(self.batch_id, "batch_id")
        if self.intent not in {"create", "open_existing", "relocate", "cloned_as_new"}:
            raise BatchProjectError("designation intent is invalid")
        _text(self.project_uri, "project_uri")
        _sha(self.authorizing_record_sha256, "authorizing_record_sha256")
        for name in (
            "expected_project_identity_sha256",
            "expected_authority_head_sha256",
            "clone_source_project_identity_sha256",
            "clone_source_qpproj_snapshot_sha256",
            "clone_source_identity_sidecar_sha256",
            "clone_source_entry_projection_set_sha256",
            "clone_required_saved_state_set_sha256",
        ):
            _sha(getattr(self, name), name, optional=True)
        if self.relocation_from_uri is not None:
            _text(self.relocation_from_uri, "relocation_from_uri")
        if self.clone_source_project_uri is not None:
            _text(self.clone_source_project_uri, "clone_source_project_uri")
        clone_fields = (
            self.clone_source_project_uri,
            self.clone_source_project_identity_sha256,
            self.clone_source_qpproj_snapshot_sha256,
            self.clone_source_identity_sidecar_sha256,
            self.clone_source_entry_projection_set_sha256,
            self.clone_required_saved_state_set_sha256,
        )
        if self.intent == "cloned_as_new":
            if any(item is None for item in clone_fields):
                raise BatchProjectError("clone designation requires complete source evidence")
        elif any(item is not None for item in clone_fields):
            raise BatchProjectError("non-clone designation cannot carry clone evidence")
        if self.intent == "relocate":
            if self.relocation_from_uri is None:
                raise BatchProjectError("relocation requires the prior active URI")
        elif self.relocation_from_uri is not None:
            raise BatchProjectError("non-relocation request cannot carry prior URI")
        if _sha(self.request_sha256, "request_sha256") != _digest(self._unsigned_mapping()):
            raise BatchProjectError("designation request checksum is invalid")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name != "request_sha256"
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "request_sha256": self.request_sha256}

    @classmethod
    def create(cls, **values: object) -> "ProjectDesignationRequest":
        unsigned = {
            "schema_name": "mif-qupath-project-designation-request",
            "schema_version": 1,
            **values,
        }
        return cls(**unsigned, request_sha256=_digest(unsigned))  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "ProjectDesignationRequest":
        return cls(**dict(_closed(value, frozenset(cls.__dataclass_fields__), "designation request")))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class AnnotationClassifierProjectRequest:
    """State-issued request to mutate one existing project classifier."""

    schema_name: Literal["mif-annotation-classifier-project-request"]
    schema_version: Literal[1]
    batch_id: str
    intent: Literal["open_existing"]
    project_uri: str
    authorizing_record_sha256: str
    expected_project_identity_sha256: str
    expected_authority_head_sha256: str
    request_sha256: str

    def __post_init__(self) -> None:
        if (
            self.schema_name != "mif-annotation-classifier-project-request"
            or self.schema_version != 1
        ):
            raise BatchProjectError("annotation-classifier project request schema is unsupported")
        _text(self.batch_id, "batch_id")
        if self.intent != "open_existing":
            raise BatchProjectError(
                "annotation-classifier project request must open an existing project"
            )
        _text(self.project_uri, "project_uri")
        for name in (
            "authorizing_record_sha256",
            "expected_project_identity_sha256",
            "expected_authority_head_sha256",
        ):
            _sha(getattr(self, name), name)
        if _sha(self.request_sha256, "request_sha256") != _digest(self._unsigned_mapping()):
            raise BatchProjectError("annotation-classifier project request checksum is invalid")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name != "request_sha256"
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "request_sha256": self.request_sha256}

    @classmethod
    def create(cls, **values: object) -> "AnnotationClassifierProjectRequest":
        unsigned = {
            "schema_name": "mif-annotation-classifier-project-request",
            "schema_version": 1,
            **values,
        }
        return cls(  # type: ignore[arg-type]
            **unsigned,
            request_sha256=_digest(unsigned),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "AnnotationClassifierProjectRequest":
        return cls(  # type: ignore[arg-type]
            **dict(
                _closed(
                    value,
                    frozenset(cls.__dataclass_fields__),
                    "annotation-classifier project request",
                )
            )
        )


@dataclass(frozen=True, slots=True)
class BatchRegistrationProjectRequest:
    """State-issued request for one existing-project Warpy evidence attempt."""

    schema_name: Literal["mif-batch-registration-project-request"]
    schema_version: Literal[1]
    batch_id: str
    node_id: str
    attempt_id: str
    attempt_authorization_sha256: str
    execution_context_sha256: str
    state_head_sha256: str
    active_plan_sha256: str
    workspace_storage_scope_sha256: str
    attempt_kind: Literal[
        "initial_registration_import",
        "import_warpy_transform",
        "review_registration",
        "accept_registration_and_transfer",
    ]
    intent: Literal["open_existing"]
    project_uri: str
    project_identity_sha256: str
    project_authority_head_sha256: str
    project_qpproj_snapshot_sha256: str
    project_catalogue_sha256: str
    fixed_project_entry_id: str
    fixed_entry_projection_sha256: str
    fixed_image_sha256: str
    moving_project_entry_id: str
    moving_entry_projection_sha256: str
    moving_image_sha256: str
    moving_reference_modality: str
    analysis_frame_ref_sha256: str
    tissue_acceptance_result_ref_sha256: str
    tissue_generation_id: str
    tissue_generation_content_sha256: str
    tissue_section_inventory_sha256: str
    tissue_section_ids_sha256: str
    tissue_roi_ids_sha256: str
    tissue_path_object_ids_sha256: str
    snapshot_resolution_sha256: str
    transform_uri: str
    transform_sha256: str
    interaction_response_sha256: str | None
    predecessor_terminal_record_sha256: str | None
    predecessor_transform_snapshot_resolution_sha256: str | None
    registration_predecessor_sha256: str | None
    ordered_roles: tuple[Literal["registration_import", "transfer_qc"], ...]
    registration_exchange_id: str
    runtime_target_plan_sha256: str
    authorizing_record_sha256: str
    request_sha256: str

    def __post_init__(self) -> None:
        """Validate the BatchRegistrationProjectRequest fields when the record is constructed.

        The checks cover ``fixed_project_entry_id``, ``moving_project_entry_id``,
        ``intent``, ``attempt_kind``, ``ordered_roles``, ``interaction_response_sha256``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        if self.schema_name != "mif-batch-registration-project-request" or self.schema_version != 1:
            raise BatchProjectError("registration project request schema is unsupported")
        for name in (
            "batch_id",
            "node_id",
            "attempt_id",
            "fixed_project_entry_id",
            "moving_project_entry_id",
            "moving_reference_modality",
            "tissue_generation_id",
        ):
            _text(getattr(self, name), name)
        if self.fixed_project_entry_id == self.moving_project_entry_id:
            raise BatchProjectError("registration fixed and moving entries must be distinct")
        if self.intent != "open_existing":
            raise BatchProjectError("registration project request must open an existing project")
        parsed_project = urlparse(_text(self.project_uri, "project_uri"))
        if (
            parsed_project.scheme != "file"
            or parsed_project.netloc not in {"", "localhost"}
            or not parsed_project.path.endswith(".qpproj")
        ):
            raise BatchProjectError("registration project request requires one local qpproj URI")
        parsed_transform = urlparse(_text(self.transform_uri, "transform_uri"))
        expected_transform_name = (
            f"transform_{self.fixed_project_entry_id}_" f"{self.moving_project_entry_id}.json"
        )
        if (
            parsed_transform.scheme != "file"
            or parsed_transform.netloc not in {"", "localhost"}
            or Path(unquote(parsed_transform.path)).name != expected_transform_name
        ):
            raise BatchProjectError("registration transform URI differs from the exact entry pair")
        for name in (
            "attempt_authorization_sha256",
            "execution_context_sha256",
            "state_head_sha256",
            "active_plan_sha256",
            "workspace_storage_scope_sha256",
            "project_identity_sha256",
            "project_authority_head_sha256",
            "project_qpproj_snapshot_sha256",
            "project_catalogue_sha256",
            "fixed_entry_projection_sha256",
            "fixed_image_sha256",
            "moving_entry_projection_sha256",
            "moving_image_sha256",
            "analysis_frame_ref_sha256",
            "tissue_acceptance_result_ref_sha256",
            "tissue_generation_content_sha256",
            "tissue_section_inventory_sha256",
            "tissue_section_ids_sha256",
            "tissue_roi_ids_sha256",
            "tissue_path_object_ids_sha256",
            "snapshot_resolution_sha256",
            "transform_sha256",
            "registration_exchange_id",
            "runtime_target_plan_sha256",
            "authorizing_record_sha256",
        ):
            _sha(getattr(self, name), name)
        for name in (
            "interaction_response_sha256",
            "predecessor_terminal_record_sha256",
            "predecessor_transform_snapshot_resolution_sha256",
            "registration_predecessor_sha256",
        ):
            _sha(getattr(self, name), name, optional=True)
        expected_roles = {
            "initial_registration_import": ("registration_import",),
            "import_warpy_transform": ("registration_import",),
            "review_registration": ("registration_import",),
            "accept_registration_and_transfer": (
                "registration_import",
                "transfer_qc",
            ),
        }.get(self.attempt_kind)
        if self.ordered_roles != expected_roles:
            raise BatchProjectError("registration request roles differ from its attempt kind")
        retry_fields = (
            self.interaction_response_sha256,
            self.predecessor_terminal_record_sha256,
            self.predecessor_transform_snapshot_resolution_sha256,
        )
        if self.attempt_kind == "initial_registration_import":
            if any(
                item is not None for item in (*retry_fields, self.registration_predecessor_sha256)
            ):
                raise BatchProjectError("initial registration request cannot claim a predecessor")
        elif self.attempt_kind == "import_warpy_transform":
            if (
                any(item is None for item in retry_fields)
                or self.registration_predecessor_sha256 is not None
            ):
                raise BatchProjectError(
                    "Warpy import retry requires only its exact transform-choice predecessor"
                )
        elif self.attempt_kind in {"review_registration", "accept_registration_and_transfer"}:
            if (
                self.interaction_response_sha256 is None
                or self.predecessor_terminal_record_sha256 is None
                or self.predecessor_transform_snapshot_resolution_sha256 is not None
                or self.registration_predecessor_sha256 is None
            ):
                raise BatchProjectError(
                    "reviewed registration request requires its exact review predecessor"
                )
        else:
            raise BatchProjectError("registration request attempt kind is unsupported")
        if _sha(self.request_sha256, "request_sha256") != _digest(self._unsigned_mapping()):
            raise BatchProjectError("registration project request checksum is invalid")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            name: list(value) if name == "ordered_roles" else value
            for name in self.__dataclass_fields__
            if name != "request_sha256"
            for value in (getattr(self, name),)
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "request_sha256": self.request_sha256}

    @classmethod
    def create(cls, **values: object) -> "BatchRegistrationProjectRequest":
        expected = frozenset(cls.__dataclass_fields__).difference(
            {"schema_name", "schema_version", "request_sha256"}
        )
        if frozenset(values) != expected:
            raise BatchProjectError("registration project request factory fields are not closed")
        unsigned = {
            "schema_name": "mif-batch-registration-project-request",
            "schema_version": 1,
            **values,
        }
        wire = {
            name: list(value) if name == "ordered_roles" else value
            for name, value in unsigned.items()
        }
        return cls(  # type: ignore[arg-type]
            **unsigned,
            request_sha256=_digest(wire),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "BatchRegistrationProjectRequest":
        material = dict(
            _closed(
                value,
                frozenset(cls.__dataclass_fields__),
                "registration project request",
            )
        )
        roles = material.get("ordered_roles")
        if type(roles) is not list:
            raise BatchProjectError("registration request roles must be an array")
        material["ordered_roles"] = tuple(roles)
        return cls(**material)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class EnsureBatchEntriesRequest:
    schema_name: Literal["mif-batch-project-upsert-request"]
    schema_version: Literal[1]
    batch_id: str
    designation: ProjectDesignation
    sample_id: str
    slide_id: str
    validation_domain_id: str
    primary_processed_image: BatchArtifactRef
    moving_reference: MovingReferenceAuthority
    expected_catalogue_sha256: str | None
    request_sha256: str
    preserved_primary: BatchProjectEntry | None = None

    def __post_init__(self) -> None:
        if self.schema_name != "mif-batch-project-upsert-request" or self.schema_version != 1:
            raise BatchProjectError("project-upsert request schema is unsupported")
        for name in ("batch_id", "sample_id", "slide_id", "validation_domain_id"):
            _text(getattr(self, name), name)
        if not isinstance(self.designation, ProjectDesignation):
            raise BatchProjectError("request designation is invalid")
        if (
            not isinstance(self.primary_processed_image, BatchArtifactRef)
            or self.primary_processed_image.artifact_kind != "processed_image"
        ):
            raise BatchProjectError("request requires one processed OME-Zarr artifact")
        if not isinstance(
            self.moving_reference, (NoMovingReferenceAuthority, PairedMovingReferenceAuthority)
        ):
            raise BatchProjectError("moving-reference authority is invalid")
        _sha(self.expected_catalogue_sha256, "expected_catalogue_sha256", optional=True)
        if self.preserved_primary is not None:
            primary = self.preserved_primary
            if (type(primary) is not BatchProjectEntry or primary.image_role != "primary_mif"
                    or (primary.sample_id, primary.slide_id, primary.validation_domain_id)
                    != (self.sample_id, self.slide_id, self.validation_domain_id)
                    or primary.canonical_uri != self.primary_source_uri
                    or primary.image_sha256 != self.primary_processed_image.sha256
                    or primary.saved_state_attestation_sha256 is None
                    or self.expected_catalogue_sha256 is None
                    or not isinstance(self.moving_reference, PairedMovingReferenceAuthority)):
                raise BatchProjectError("Moving-only preparation requires its unchanged current primary image.")
        if _sha(self.request_sha256, "request_sha256") != _digest(self._unsigned_mapping()):
            raise BatchProjectError("project-upsert request checksum is not canonical")

    @property
    def primary_source_uri(self) -> str:
        # We address the completed image directory exactly as Java Path.toUri does.
        return self.primary_processed_image.locator.resolve().as_uri() + "/"

    @property
    def moving_only(self) -> bool:
        """A retained primary binds this request to moving-image publication only."""
        return self.preserved_primary is not None

    def _unsigned_mapping(self) -> dict[str, object]:
        result = {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "batch_id": self.batch_id,
            "designation": {
                "project_path": self.designation.project_path.as_posix(),
                "project_uri": self.designation.project_uri,
                "identity": self.designation.identity.to_mapping(),
                "authority_head": self.designation.authority_head.to_mapping(),
            },
            "sample_id": self.sample_id,
            "slide_id": self.slide_id,
            "validation_domain_id": self.validation_domain_id,
            "primary_processed_image": self.primary_processed_image.to_mapping(),
            "moving_reference": self.moving_reference.to_mapping(),
            "expected_catalogue_sha256": self.expected_catalogue_sha256,
        }
        if self.preserved_primary is not None:
            result["preserved_primary"] = self.preserved_primary.to_mapping()
        return result

    @classmethod
    def create(cls, **values: object) -> "EnsureBatchEntriesRequest":
        prototype = object.__new__(cls)
        unsigned = {
            "schema_name": "mif-batch-project-upsert-request",
            "schema_version": 1,
            **values,
        }
        unsigned.setdefault("preserved_primary", None)
        for name, item in unsigned.items():
            object.__setattr__(prototype, name, item)
        return cls(**unsigned, request_sha256=_digest(prototype._unsigned_mapping()))  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "request_sha256": self.request_sha256}


@dataclass(frozen=True, slots=True)
class BatchProjectEntry:
    project_entry_id: str
    sample_id: str
    slide_id: str
    validation_domain_id: str
    image_role: Literal["primary_mif", "moving_reference"]
    reference_modality: str | None
    paired_entry_id: str | None
    canonical_uri: str
    image_sha256: str
    saved_state_attestation_sha256: str | None
    entry_projection_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "project_entry_id",
            "sample_id",
            "slide_id",
            "validation_domain_id",
            "canonical_uri",
        ):
            _text(getattr(self, name), name)
        if self.image_role not in {"primary_mif", "moving_reference"}:
            raise BatchProjectError("image role is invalid")
        if self.image_role == "primary_mif" and self.reference_modality is not None:
            raise BatchProjectError("primary entry cannot have a reference modality")
        if self.image_role == "moving_reference" and self.reference_modality is None:
            raise BatchProjectError("moving reference requires reviewed modality")
        if self.reference_modality is not None:
            _text(self.reference_modality, "reference_modality")
        if self.paired_entry_id is not None:
            _text(self.paired_entry_id, "paired_entry_id")
        _sha(self.image_sha256, "image_sha256")
        _sha(self.saved_state_attestation_sha256, "saved_state_attestation_sha256", optional=True)
        if _sha(self.entry_projection_sha256, "entry_projection_sha256") != _digest(
            self._unsigned_mapping()
        ):
            raise BatchProjectError("entry projection checksum is not canonical")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name != "entry_projection_sha256"
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "entry_projection_sha256": self.entry_projection_sha256}

    @classmethod
    def create(cls, **values: object) -> "BatchProjectEntry":
        return cls(**values, entry_projection_sha256=_digest(values))  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "BatchProjectEntry":
        return cls(**dict(_closed(value, frozenset(cls.__dataclass_fields__), "batch project entry")))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class BatchProjectCatalogue:
    schema_name: Literal["mif-batch-project-catalogue"]
    schema_version: Literal[1]
    batch_id: str
    project_identity_sha256: str
    project_uri: str
    project_authority_predecessor_head_sha256: str
    qpproj_snapshot_sha256: str
    predecessor_catalogue_sha256: str | None
    mutation_request_sha256: str
    mutation_authorization_sha256: str
    entries: tuple[BatchProjectEntry, ...]
    catalogue_sha256: str

    def __post_init__(self) -> None:
        if self.schema_name != "mif-batch-project-catalogue" or self.schema_version != 1:
            raise BatchProjectError("project catalogue schema is unsupported")
        _text(self.batch_id, "batch_id")
        _text(self.project_uri, "project_uri")
        for name in (
            "project_identity_sha256",
            "project_authority_predecessor_head_sha256",
            "qpproj_snapshot_sha256",
            "mutation_request_sha256",
            "mutation_authorization_sha256",
        ):
            _sha(getattr(self, name), name)
        _sha(self.predecessor_catalogue_sha256, "predecessor_catalogue_sha256", optional=True)
        entries = tuple(
            sorted(
                self.entries,
                key=lambda item: (item.sample_id, item.image_role, item.project_entry_id),
            )
        )
        if not all(isinstance(item, BatchProjectEntry) for item in entries):
            raise BatchProjectError("catalogue entries must be typed")
        if len({item.project_entry_id for item in entries}) != len(entries):
            raise BatchProjectError("catalogue contains duplicate project entry IDs")
        by_sample: dict[str, list[BatchProjectEntry]] = {}
        for entry in entries:
            by_sample.setdefault(entry.sample_id, []).append(entry)
        for sample_entries in by_sample.values():
            primary = [entry for entry in sample_entries if entry.image_role == "primary_mif"]
            references = [
                entry for entry in sample_entries if entry.image_role == "moving_reference"
            ]
            if len(primary) != 1 or len(references) > 1:
                raise BatchProjectError(
                    "each catalogue sample requires one primary and at most one reference"
                )
            if references:
                if (
                    primary[0].paired_entry_id != references[0].project_entry_id
                    or references[0].paired_entry_id != primary[0].project_entry_id
                ):
                    raise BatchProjectError("catalogue pairing must be reciprocal")
            elif primary[0].paired_entry_id is not None:
                raise BatchProjectError("unpaired primary cannot name a reference")
        object.__setattr__(self, "entries", entries)
        if _sha(self.catalogue_sha256, "catalogue_sha256") != _digest(self._unsigned_mapping()):
            raise BatchProjectError("catalogue checksum is not canonical")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "batch_id": self.batch_id,
            "project_identity_sha256": self.project_identity_sha256,
            "project_uri": self.project_uri,
            "project_authority_predecessor_head_sha256": self.project_authority_predecessor_head_sha256,
            "qpproj_snapshot_sha256": self.qpproj_snapshot_sha256,
            "predecessor_catalogue_sha256": self.predecessor_catalogue_sha256,
            "mutation_request_sha256": self.mutation_request_sha256,
            "mutation_authorization_sha256": self.mutation_authorization_sha256,
            "entries": [entry.to_mapping() for entry in self.entries],
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "catalogue_sha256": self.catalogue_sha256}

    @classmethod
    def create(cls, **values: object) -> "BatchProjectCatalogue":
        entries = tuple(sorted(values["entries"], key=lambda item: (item.sample_id, item.image_role, item.project_entry_id)))  # type: ignore[index,union-attr]
        unsigned = {
            "schema_name": "mif-batch-project-catalogue",
            "schema_version": 1,
            **values,
            "entries": entries,
        }
        prototype = object.__new__(cls)
        for name, item in unsigned.items():
            object.__setattr__(prototype, name, item)
        return cls(**unsigned, catalogue_sha256=_digest(prototype._unsigned_mapping()))  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "BatchProjectCatalogue":
        material = dict(
            _closed(value, frozenset(cls.__dataclass_fields__), "batch project catalogue")
        )
        raw_entries = material["entries"]
        if not isinstance(raw_entries, list):
            raise BatchProjectError("catalogue entries must be an array")
        material["entries"] = tuple(BatchProjectEntry.from_mapping(item) for item in raw_entries)
        return cls(**material)  # type: ignore[arg-type]

    def resolve(self, project_entry_id: str) -> BatchProjectEntry:
        matches = [entry for entry in self.entries if entry.project_entry_id == project_entry_id]
        if len(matches) != 1:
            raise KeyError(project_entry_id)
        return matches[0]


def project_catalogue_entry_owning_identity(
    catalogue: BatchProjectCatalogue,
    project_entry_id: str,
) -> Mapping[str, object]:
    """Project one entry's complete identity from its owning catalogue."""

    if not isinstance(catalogue, BatchProjectCatalogue):
        raise BatchProjectError("project catalogue entry identity requires a typed catalogue")
    try:
        entry = catalogue.resolve(_text(project_entry_id, "project_entry_id"))
    except KeyError as exc:
        raise BatchProjectError("project catalogue entry identity is absent") from exc
    return {
        "project_entry_id": entry.project_entry_id,
        "project_identity_sha256": catalogue.project_identity_sha256,
        "entry_projection_sha256": entry.entry_projection_sha256,
        "canonical_image_sha256": entry.image_sha256,
        "saved_state_attestation_sha256": (entry.saved_state_attestation_sha256),
        "catalogue_sha256": catalogue.catalogue_sha256,
    }


def _validate_project_catalogue_entry_ref_contract(
    ref: BatchArtifactRef,
) -> BatchArtifactRef:
    try:
        validated = validate_artifact_ref(ref)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchProjectError("project catalogue entry artifact reference is invalid") from exc
    contract = project_catalogue_entry_identity_contract()
    if (
        validated.identity_contract != contract
        or validated.owning_identity_contract_sha256 != contract.contract_sha256
        or validated.artifact_kind != "project_catalogue_entry"
        or (validated.schema_name, validated.schema_version) != ("mif-batch-project-catalogue", 1)
        or validated.producer_version != _PROJECT_CATALOGUE_ENTRY_PRODUCER_VERSION
        or dict(validated.runtime_versions) != {"mif_pipeline": _MIF_PIPELINE_BATCH_RUNTIME_VERSION}
    ):
        raise BatchProjectError("project catalogue entry artifact lacks its public owner contract")
    return validated


def reopen_project_catalogue_entry_artifact(
    ref: BatchArtifactRef,
) -> tuple[BatchProjectCatalogue, BatchProjectEntry]:
    """Reopen the bounded canonical catalogue bytes named by one entry ref."""

    validated = _validate_project_catalogue_entry_ref_contract(ref)
    if (
        validated.locator is None
        or validated.sha256 is None
        or validated.byte_count is None
        or not validated.locator.is_absolute()
        or validated.byte_count > _CATALOGUE_RECORD_MAX_BYTES
    ):
        raise BatchProjectError("project catalogue entry artifact lacks bounded located bytes")
    try:
        payload, mapping = read_canonical_record(
            validated.locator,
            max_bytes=_CATALOGUE_RECORD_MAX_BYTES,
            label="project catalogue entry artifact",
        )
        if (
            len(payload) != validated.byte_count
            or hashlib.sha256(payload).hexdigest() != validated.sha256
        ):
            raise BatchProjectError("project catalogue entry artifact bytes differ")
        catalogue = BatchProjectCatalogue.from_mapping(mapping)
        project_entry_id = validated.owning_identity.get("project_entry_id")
        entry = catalogue.resolve(_text(project_entry_id, "project_entry_id"))
    except BatchProjectError:
        raise
    except (DurableRecordError, KeyError, TypeError, ValueError) as exc:
        raise BatchProjectError("project catalogue entry artifact cannot be reopened") from exc
    if (
        dict(validated.owning_identity)
        != dict(
            project_catalogue_entry_owning_identity(
                catalogue,
                entry.project_entry_id,
            )
        )
        or validated.scope
        != ArtifactScope(
            entry.validation_domain_id,
            entry.sample_id,
            entry.slide_id,
            entry.project_entry_id,
            (),
        )
        or len(validated.configuration_bindings) != 1
    ):
        raise BatchProjectError("project catalogue entry artifact projection differs")
    return catalogue, entry


@dataclass(frozen=True, slots=True)
class CurrentProjectEntryChannelObservation:
    """State-bound ordered channel names observed from the retained qpproj."""

    state_head_sha256: str
    project_entry_id: str
    entry_projection_sha256: str
    qpproj_snapshot_sha256: str
    ordered_channel_names: tuple[str, ...]

    def __post_init__(self) -> None:
        _sha(self.state_head_sha256, "state_head_sha256")
        _text(self.project_entry_id, "project_entry_id")
        _sha(self.entry_projection_sha256, "entry_projection_sha256")
        _sha(self.qpproj_snapshot_sha256, "qpproj_snapshot_sha256")
        names = tuple(
            _text(item, f"ordered_channel_names[{index}]")
            for index, item in enumerate(self.ordered_channel_names)
        )
        if not names:
            raise BatchProjectError("live project channels must be nonempty")
        object.__setattr__(self, "ordered_channel_names", names)


def _catalogue_witnesses_same_primary_image(
    source_catalogue: BatchProjectCatalogue,
    source_entry: BatchProjectEntry,
    current_catalogue: BatchProjectCatalogue | None,
) -> bool:
    """Keep the accepted primary image across later pair and hierarchy changes.

    Both catalogues are already authenticated by their callers. Image identity
    stays fixed; the current head and saved-state owner separately bind today's
    pair association and hierarchy. This does not admit a replacement primary.
    """

    if (
        current_catalogue is None
        or source_catalogue.batch_id != current_catalogue.batch_id
        or source_catalogue.project_identity_sha256 != current_catalogue.project_identity_sha256
    ):
        return False
    try:
        current_entry = current_catalogue.resolve(source_entry.project_entry_id)
        identity_fields = (
            "project_entry_id", "sample_id", "slide_id", "validation_domain_id",
            "image_role", "reference_modality", "canonical_uri", "image_sha256",
        )
        return source_entry.image_role == "primary_mif" and all(
            getattr(current_entry, field) == getattr(source_entry, field)
            for field in identity_fields
        )
    except KeyError:
        return False


def reopen_current_project_entry_channel_observation(
    *,
    state_store: BatchStateStore,
    expected_head_sha256: str,
    catalogue_store: "BatchProjectCatalogueStore",
    project_entry_id: str,
) -> CurrentProjectEntryChannelObservation:
    """Observe live QuPath channel order from the current retained project."""

    if (
        type(state_store) is not BatchStateStore
        or type(catalogue_store) is not BatchProjectCatalogueStore
    ):
        raise BatchProjectError(
            "project channel observation requires exact state and catalogue owners"
        )
    expected_head = _sha(expected_head_sha256, "expected_head_sha256")
    entry_id = _text(project_entry_id, "project_entry_id")
    head = state_store.load_head()
    if head.sha256 != expected_head:
        raise BatchProjectError("project channel observation head is stale")
    snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=expected_head)
    project_refs = tuple(
        ref
        for ref in snapshot.accepted_artifacts
        if ref.artifact_kind == "project_catalogue_entry"
        and ref.scope.project_entry_id == entry_id
        and ref.owning_identity["entry_projection_sha256"]
        == head.sample_project_entry_projection_sha256_by_sample.get(ref.scope.sample_id)
    )
    if len(project_refs) != 1:
        raise BatchProjectError("project channel observation lacks one current entry")
    source_catalogue, entry = reopen_project_catalogue_entry_artifact(project_refs[0])
    frame_refs = tuple(
        ref
        for ref in snapshot.accepted_artifacts
        if ref.artifact_kind == "analysis_frame" and ref.scope.sample_id == entry.sample_id
    )
    if len(frame_refs) != 1:
        raise BatchProjectError("project channel observation lacks one current analysis frame")
    frame = validate_analysis_frame_artifact_ref(frame_refs[0])
    catalogue = catalogue_store.load_current()
    if (
        catalogue is None
        or source_catalogue.project_identity_sha256 != catalogue.project_identity_sha256
        or source_catalogue.batch_id != catalogue.batch_id
        or entry not in catalogue.entries
        or entry.image_role != "primary_mif"
        or frame.spatial_frame.canonical_image_sha256 != entry.image_sha256
        or head.project_materialization_state != "materialized"
        or head.project_identity_sha256 != catalogue.project_identity_sha256
        or head.project_catalogue_sha256 != catalogue.catalogue_sha256
        or head.sample_project_entry_id_by_sample.get(entry.sample_id) != entry_id
        or head.sample_project_entry_projection_sha256_by_sample.get(entry.sample_id)
        != entry.entry_projection_sha256
    ):
        raise BatchProjectError(
            "project channel observation differs from current project authority"
        )
    try:
        with retained_project_file(Path(head.project_path)) as retained:
            observed = parse_project_entry_pyramid_payload(
                retained.payload,
                project_uri=catalogue.project_uri,
                project_entry_id=entry.project_entry_id,
                batch_id=catalogue.batch_id,
                sample_id=entry.sample_id,
                slide_id=entry.slide_id,
                validation_domain_id=entry.validation_domain_id,
                canonical_image_uri=entry.canonical_uri,
                canonical_image_sha256=entry.image_sha256,
                level0_size_x_px=frame.spatial_frame.level0_size_x_px,
                level0_size_y_px=frame.spatial_frame.level0_size_y_px,
                frame_x_um_per_px=frame.spatial_frame.x_um_per_px,
                frame_y_um_per_px=frame.spatial_frame.y_um_per_px,
            )
            if (
                not same_project_snapshot(
                    Path(head.project_path),
                    head.project_qpproj_snapshot_sha256,
                    retained.sha256,
                    observed_payload=retained.payload,
                )
                or observed.qpproj_snapshot_sha256 != retained.sha256
            ):
                raise BatchProjectError("project channel observation qpproj is stale")
            retained.validate_unchanged()
        builder = json.loads(observed.server_builder_json)
        channels = builder["metadata"]["channels"]
        names = tuple(item["name"] for item in channels)
    except BatchProjectError:
        raise
    except (KeyError, OSError, ProjectPyramidContractError, TypeError, ValueError) as exc:
        raise BatchProjectError("project channel observation cannot reopen live metadata") from exc
    return CurrentProjectEntryChannelObservation(
        state_head_sha256=head.sha256,
        project_entry_id=entry.project_entry_id,
        entry_projection_sha256=entry.entry_projection_sha256,
        qpproj_snapshot_sha256=head.project_qpproj_snapshot_sha256,
        ordered_channel_names=names,
    )


def build_project_entry_pyramid_request(
    context: BatchNodeExecutionContext,
    *,
    execution_authority: BatchCurrentExecutionAuthority,
    analysis_frame_authority: BatchCommittedArtifactAuthority,
    project_catalogue_entry_authority: BatchCommittedArtifactAuthority,
    current_state_head: BatchStateHead,
    state_store: BatchStateStore,
    saved_state_before_sha256: str,
) -> ProjectEntryPyramidRequest:
    """Bind a targeted read-only request to current state and catalogue owners."""

    if (
        type(context) is not BatchNodeExecutionContext
        or type(execution_authority) is not BatchCurrentExecutionAuthority
        or type(analysis_frame_authority) is not BatchCommittedArtifactAuthority
        or type(project_catalogue_entry_authority) is not BatchCommittedArtifactAuthority
        or type(current_state_head) is not BatchStateHead
        or type(state_store) is not BatchStateStore
    ):
        raise BatchProjectError("project-entry pyramid request lacks exact owner authority")
    try:
        execution = validate_current_execution_authority(
            execution_authority,
            expected_context=context,
        )
        sources = validate_committed_artifact_authority_set(
            (analysis_frame_authority, project_catalogue_entry_authority),
            expected_active_plan_sha256=execution.active_plan_sha256,
        )
        frame = validate_analysis_frame_artifact_ref(analysis_frame_authority.artifact_ref)
        source_catalogue, source_entry = reopen_project_catalogue_entry_artifact(
            project_catalogue_entry_authority.artifact_ref
        )
        current_catalogue = state_store.reopen_current_project_catalogue(
            expected_head_sha256=current_state_head.sha256
        )
        current_entry = current_catalogue.resolve(source_entry.project_entry_id)
    except (
        AttributeError,
        KeyError,
        BatchArtifactError,
        ProjectPyramidContractError,
        TypeError,
        ValueError,
    ) as exc:
        raise BatchProjectError("project-entry pyramid owner authority cannot be reopened") from exc
    source_by_kind = {item.artifact_ref.artifact_kind: item for item in sources}
    input_by_kind = {item.artifact_kind: item for item in context.inputs}
    sample_id = source_entry.sample_id
    # The issued task includes later annotation and processing saves; the
    # catalogue entry retains the original image-registration checkpoint.
    saved_state = _sha(saved_state_before_sha256, "saved_state_before_sha256")
    if (
        current_catalogue is None
        or not _catalogue_witnesses_same_primary_image(
            source_catalogue, source_entry, current_catalogue
        )
        or len(source_by_kind) != 2
        or set(source_by_kind) != {"analysis_frame", "project_catalogue_entry"}
        or len(input_by_kind) != 2
        or set(input_by_kind) != set(source_by_kind)
        or any(source_by_kind[kind].artifact_ref != input_by_kind[kind] for kind in input_by_kind)
        or any(item.state_head_sha256 != execution.state_head_sha256 for item in sources)
        or analysis_frame_authority.producer_node_kind != "analysis_preflight"
        or project_catalogue_entry_authority.producer_node_kind != "project_upsert"
        or analysis_frame_authority.execution_exchange_sha256 is not None
        or project_catalogue_entry_authority.execution_exchange_sha256 is not None
        or execution.node_kind != "tissue_setup"
        or execution.node_scope != "sample"
        or execution.validation_domain_id != source_entry.validation_domain_id
        or execution.participant_ids != (sample_id,)
        or execution.project_entry_ids != (source_entry.project_entry_id,)
        or execution.analysis_domain_bindings
        or execution.state_head_sha256 != current_state_head.sha256
        or execution.active_plan_sha256 != current_state_head.active_plan_sha256
        or current_state_head.batch_id != current_catalogue.batch_id
        or current_state_head.project_materialization_state != "materialized"
        or current_state_head.project_identity_sha256 != current_catalogue.project_identity_sha256
        or current_state_head.project_catalogue_sha256 != current_catalogue.catalogue_sha256
        or canonical_project_uri(Path(current_state_head.project_path))
        != current_catalogue.project_uri
        or current_state_head.sample_project_entry_id_by_sample.get(sample_id)
        != source_entry.project_entry_id
        or current_state_head.sample_project_entry_projection_sha256_by_sample.get(sample_id)
        != current_entry.entry_projection_sha256
        or len(analysis_frame_authority.artifact_ref.configuration_bindings) != 1
        or current_state_head.resolved_config_sha256_by_sample.get(sample_id)
        != analysis_frame_authority.artifact_ref.configuration_bindings[0].resolved_config_sha256
        or analysis_frame_authority.artifact_ref.scope.sample_id != sample_id
        or analysis_frame_authority.artifact_ref.scope.slide_id != source_entry.slide_id
        or analysis_frame_authority.artifact_ref.scope.validation_domain_id
        != source_entry.validation_domain_id
        or frame.spatial_frame.canonical_image_sha256 != source_entry.image_sha256
        or source_entry.image_role != "primary_mif"
        or context.authorization.authorization_sha256 != execution.authorization_sha256
    ):
        raise BatchProjectError(
            "project-entry pyramid authority differs from current state or catalogue"
        )
    frame_evidence = frame.spatial_frame
    try:
        with retained_project_file(Path(current_state_head.project_path)) as retained:
            observation = parse_project_entry_pyramid_payload(
                retained.payload,
                project_uri=current_catalogue.project_uri,
                project_entry_id=source_entry.project_entry_id,
                batch_id=current_catalogue.batch_id,
                sample_id=source_entry.sample_id,
                slide_id=source_entry.slide_id,
                validation_domain_id=source_entry.validation_domain_id,
                canonical_image_uri=source_entry.canonical_uri,
                canonical_image_sha256=source_entry.image_sha256,
                level0_size_x_px=frame_evidence.level0_size_x_px,
                level0_size_y_px=frame_evidence.level0_size_y_px,
                frame_x_um_per_px=frame_evidence.x_um_per_px,
                frame_y_um_per_px=frame_evidence.y_um_per_px,
            )
            if (
                # The catalogue can predate a display-only project save. Both
                # recorded versions must retain the observed image inputs.
                not all(
                    same_project_snapshot(
                        Path(current_state_head.project_path),
                        expected_snapshot,
                        retained.sha256,
                        observed_payload=retained.payload,
                    )
                    for expected_snapshot in (
                        current_state_head.project_qpproj_snapshot_sha256,
                        current_catalogue.qpproj_snapshot_sha256,
                    )
                )
                or observation.qpproj_snapshot_sha256 != retained.sha256
                or observation.parser_profile_id != PROJECT_PYRAMID_PARSER_PROFILE_ID
            ):
                raise BatchProjectError(
                    "project-entry pyramid qpproj differs from current catalogue"
                )
            retained.validate_unchanged()
    except BatchProjectError:
        raise
    except ProjectPyramidScientificIncompatibility as exc:
        raise BatchProjectError(
            "project-entry pyramid metadata is scientifically incompatible"
        ) from exc
    except (OSError, ProjectPyramidContractError, ValueError) as exc:
        raise BatchProjectError("project-entry pyramid metadata is contract incompatible") from exc
    return ProjectEntryPyramidRequest.create(
        batch_id=current_catalogue.batch_id,
        state_head_sha256=current_state_head.sha256,
        active_plan_sha256=current_state_head.active_plan_sha256,
        execution_authorization_sha256=execution.authorization_sha256,
        project_identity_sha256=current_catalogue.project_identity_sha256,
        project_catalogue_sha256=current_catalogue.catalogue_sha256,
        qpproj_snapshot_sha256=current_state_head.project_qpproj_snapshot_sha256,
        project_uri=current_catalogue.project_uri,
        project_entry_id=source_entry.project_entry_id,
        entry_projection_sha256=current_entry.entry_projection_sha256,
        sample_id=sample_id,
        slide_id=source_entry.slide_id,
        validation_domain_id=source_entry.validation_domain_id,
        canonical_image_uri=source_entry.canonical_uri,
        canonical_image_sha256=source_entry.image_sha256,
        saved_state_attestation_sha256=saved_state,
        analysis_frame_ref_sha256=(analysis_frame_authority.artifact_ref.artifact_ref_sha256),
        project_catalogue_entry_ref_sha256=(
            project_catalogue_entry_authority.artifact_ref.artifact_ref_sha256
        ),
        spatial_frame_sha256=frame_evidence.spatial_frame_sha256,
        level0_size_x_px=frame_evidence.level0_size_x_px,
        level0_size_y_px=frame_evidence.level0_size_y_px,
        frame_x_um_per_px_hex=frame_evidence.x_um_per_px.hex(),
        frame_y_um_per_px_hex=frame_evidence.y_um_per_px.hex(),
        parser_profile_id=observation.parser_profile_id,
        project_version=observation.project_version,
        server_builder_json=observation.server_builder_json,
        server_builder_sha256=observation.server_builder_sha256,
        server_builder_metadata_sha256=(observation.server_builder_metadata_sha256),
    )


def validate_current_project_entry_pyramid_evidence(
    request: ProjectEntryPyramidRequest,
    evidence: ProjectEntryPyramidEvidence,
    *,
    current_state_head: BatchStateHead,
    state_store: BatchStateStore,
) -> ProjectEntryPyramidEvidence:
    """Reauthenticate returned evidence against unchanged current owners."""

    if (
        type(request) is not ProjectEntryPyramidRequest
        or type(evidence) is not ProjectEntryPyramidEvidence
        or type(current_state_head) is not BatchStateHead
        or type(state_store) is not BatchStateStore
    ):
        raise BatchProjectError("project-entry pyramid evidence lacks exact owner authority")
    try:
        evidence.validate_request_binding(request)
        catalogue = state_store.reopen_current_project_catalogue(
            expected_head_sha256=current_state_head.sha256
        )
        entry = None if catalogue is None else catalogue.resolve(request.project_entry_id)
    except (KeyError, ProjectPyramidContractError, TypeError, ValueError) as exc:
        raise BatchProjectError("project-entry pyramid evidence cannot be reopened") from exc
    if (
        catalogue is None
        or entry is None
        or request.state_head_sha256 != current_state_head.sha256
        or request.active_plan_sha256 != current_state_head.active_plan_sha256
        or current_state_head.project_materialization_state != "materialized"
        or current_state_head.project_identity_sha256 != request.project_identity_sha256
        or current_state_head.project_qpproj_snapshot_sha256 != request.qpproj_snapshot_sha256
        or current_state_head.project_catalogue_sha256 != request.project_catalogue_sha256
        or catalogue.catalogue_sha256 != request.project_catalogue_sha256
        or catalogue.project_identity_sha256 != request.project_identity_sha256
        or not same_project_snapshot(
            Path(current_state_head.project_path),
            catalogue.qpproj_snapshot_sha256,
            request.qpproj_snapshot_sha256,
        )
        or catalogue.project_uri != request.project_uri
        or entry.entry_projection_sha256 != request.entry_projection_sha256
        or entry.sample_id != request.sample_id
        or entry.slide_id != request.slide_id
        or entry.validation_domain_id != request.validation_domain_id
        or entry.canonical_uri != request.canonical_image_uri
        or entry.image_sha256 != request.canonical_image_sha256
        or current_state_head.sample_project_entry_id_by_sample.get(request.sample_id)
        != request.project_entry_id
        or current_state_head.sample_project_entry_projection_sha256_by_sample.get(
            request.sample_id
        )
        != request.entry_projection_sha256
    ):
        raise BatchProjectError("project-entry pyramid evidence is stale against current authority")
    return evidence


def validate_project_catalogue_selection_witness(
    witness: BatchArtifactRef,
    entry_refs: tuple[BatchArtifactRef, ...],
) -> BatchProjectCatalogue:
    """Validate selected entry generations against one supplied latest catalogue."""

    if not isinstance(entry_refs, tuple) or not entry_refs:
        raise BatchProjectError(
            "project catalogue selection requires a non-empty tuple of entry refs"
        )
    witness_catalogue, _witness_entry = reopen_project_catalogue_entry_artifact(witness)
    reopened = tuple(reopen_project_catalogue_entry_artifact(ref) for ref in entry_refs)
    entry_ids = tuple(entry.project_entry_id for _catalogue, entry in reopened)
    if len(set(entry_ids)) != len(entry_ids):
        raise BatchProjectError("project catalogue selection repeats a project entry")
    for source_catalogue, selected_entry in reopened:
        if (
            source_catalogue.batch_id != witness_catalogue.batch_id
            or source_catalogue.project_identity_sha256 != witness_catalogue.project_identity_sha256
        ):
            raise BatchProjectError("project catalogue selection spans batch or project identity")
        try:
            witnessed_entry = witness_catalogue.resolve(selected_entry.project_entry_id)
        except KeyError as exc:
            raise BatchProjectError("project catalogue selection witness is stale") from exc
        if witnessed_entry != selected_entry:
            raise BatchProjectError("project catalogue selection witness entry diverges")
    return witness_catalogue


@dataclass(frozen=True, slots=True)
class BatchProjectRuntimeReplayPair:
    request_envelope_sha256: str
    request_payload_sha256: str
    request_byte_count: int
    evidence_sha256: str
    evidence_payload_sha256: str
    evidence_byte_count: int

    def __post_init__(self) -> None:
        for name in (
            "request_envelope_sha256",
            "request_payload_sha256",
            "evidence_sha256",
            "evidence_payload_sha256",
        ):
            _sha(getattr(self, name), name)
        for name in ("request_byte_count", "evidence_byte_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise BatchProjectError(f"{name} must be a positive integer")

    def to_mapping(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_mapping(cls, value: object) -> "BatchProjectRuntimeReplayPair":
        return cls(
            **dict(
                _closed(
                    value,
                    frozenset(cls.__dataclass_fields__),
                    "runtime replay pair",
                )
            )
        )  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class BatchProjectRuntimeReplay:
    schema_name: Literal["mif-batch-project-runtime-replay"]
    schema_version: Literal[1]
    mutation_request_sha256: str
    mutation_authorization_sha256: str
    catalogue: BatchProjectCatalogue
    runtime_pairs: tuple[BatchProjectRuntimeReplayPair, ...]
    replay_sha256: str

    def __post_init__(self) -> None:
        if self.schema_name != "mif-batch-project-runtime-replay" or self.schema_version != 1:
            raise BatchProjectError("runtime replay schema is unsupported")
        _sha(self.mutation_request_sha256, "mutation_request_sha256")
        _sha(self.mutation_authorization_sha256, "mutation_authorization_sha256")
        if (
            not isinstance(self.catalogue, BatchProjectCatalogue)
            or self.catalogue.mutation_request_sha256 != self.mutation_request_sha256
            or self.catalogue.mutation_authorization_sha256 != self.mutation_authorization_sha256
        ):
            raise BatchProjectError("runtime replay catalogue authority differs")
        pairs = tuple(self.runtime_pairs)
        if not pairs or not all(isinstance(pair, BatchProjectRuntimeReplayPair) for pair in pairs):
            raise BatchProjectError("runtime replay pairs are invalid")
        request_ids = [pair.request_envelope_sha256 for pair in pairs]
        evidence_ids = [pair.evidence_sha256 for pair in pairs]
        if len(set(request_ids)) != len(request_ids) or len(set(evidence_ids)) != len(evidence_ids):
            raise BatchProjectError("runtime replay pairs must be unique")
        if _sha(self.replay_sha256, "replay_sha256") != _digest(self._unsigned_mapping()):
            raise BatchProjectError("runtime replay checksum differs")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "mutation_request_sha256": self.mutation_request_sha256,
            "mutation_authorization_sha256": self.mutation_authorization_sha256,
            "catalogue": self.catalogue.to_mapping(),
            "runtime_pairs": [pair.to_mapping() for pair in self.runtime_pairs],
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "replay_sha256": self.replay_sha256}

    @classmethod
    def create(
        cls,
        *,
        catalogue: BatchProjectCatalogue,
        runtime_pairs: tuple[BatchProjectRuntimeReplayPair, ...],
    ) -> "BatchProjectRuntimeReplay":
        unsigned = {
            "schema_name": "mif-batch-project-runtime-replay",
            "schema_version": 1,
            "mutation_request_sha256": catalogue.mutation_request_sha256,
            "mutation_authorization_sha256": (catalogue.mutation_authorization_sha256),
            "catalogue": catalogue,
            "runtime_pairs": tuple(runtime_pairs),
        }
        prototype = object.__new__(cls)
        for name, item in unsigned.items():
            object.__setattr__(prototype, name, item)
        return cls(
            **unsigned,
            replay_sha256=_digest(prototype._unsigned_mapping()),
        )  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "BatchProjectRuntimeReplay":
        material = dict(
            _closed(
                value,
                frozenset(cls.__dataclass_fields__),
                "runtime replay record",
            )
        )
        material["catalogue"] = BatchProjectCatalogue.from_mapping(material["catalogue"])
        raw_pairs = material["runtime_pairs"]
        if not isinstance(raw_pairs, list):
            raise BatchProjectError("runtime replay pairs are invalid")
        material["runtime_pairs"] = tuple(
            BatchProjectRuntimeReplayPair.from_mapping(pair) for pair in raw_pairs
        )
        return cls(**material)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class BatchProjectOperationReplayStep:
    ordinal: int
    role: Literal["identity_materialization", "primary_mif", "moving_reference"]
    operation: Literal["ensure_project_identity", "upsert_entry"]
    request_identity_sha256: str
    step_sha256: str

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise BatchProjectError("operation replay ordinal must be non-negative")
        expected_operation = (
            "ensure_project_identity" if self.role == "identity_materialization" else "upsert_entry"
        )
        if (
            self.role not in {"identity_materialization", "primary_mif", "moving_reference"}
            or self.operation != expected_operation
        ):
            raise BatchProjectError("operation replay role/operation is invalid")
        _sha(self.request_identity_sha256, "request_identity_sha256")
        if _sha(self.step_sha256, "step_sha256") != _digest(self._unsigned_mapping()):
            raise BatchProjectError("operation replay step checksum differs")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "role": self.role,
            "operation": self.operation,
            "request_identity_sha256": self.request_identity_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "step_sha256": self.step_sha256}

    @classmethod
    def create(cls, **values: object) -> "BatchProjectOperationReplayStep":
        return cls(**values, step_sha256=_digest(values))  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "BatchProjectOperationReplayStep":
        return cls(
            **dict(
                _closed(
                    value,
                    frozenset(cls.__dataclass_fields__),
                    "operation replay step",
                )
            )
        )  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class BatchProjectOperationReplayPlan:
    schema_name: Literal["mif-batch-project-operation-replay-plan"]
    schema_version: Literal[1]
    mutation_request_sha256: str
    mutation_authorization_sha256: str
    steps: tuple[BatchProjectOperationReplayStep, ...]
    replay_plan_sha256: str

    def __post_init__(self) -> None:
        if (
            self.schema_name != "mif-batch-project-operation-replay-plan"
            or self.schema_version != 1
        ):
            raise BatchProjectError("operation replay plan schema is unsupported")
        _sha(self.mutation_request_sha256, "mutation_request_sha256")
        _sha(self.mutation_authorization_sha256, "mutation_authorization_sha256")
        steps = tuple(self.steps)
        if not steps or not all(
            isinstance(item, BatchProjectOperationReplayStep) for item in steps
        ):
            raise BatchProjectError("operation replay plan steps are invalid")
        if tuple(item.ordinal for item in steps) != tuple(range(len(steps))):
            raise BatchProjectError("operation replay plan order is not contiguous")
        roles = tuple(item.role for item in steps)
        allowed = {
            ("moving_reference",),
            ("primary_mif",),
            ("primary_mif", "moving_reference"),
            ("identity_materialization", "primary_mif"),
            (
                "identity_materialization",
                "primary_mif",
                "moving_reference",
            ),
        }
        if roles not in allowed:
            raise BatchProjectError("operation replay role order is invalid")
        if len({item.request_identity_sha256 for item in steps}) != len(steps):
            raise BatchProjectError("operation replay request identities must be unique")
        if _sha(self.replay_plan_sha256, "replay_plan_sha256") != _digest(self._unsigned_mapping()):
            raise BatchProjectError("operation replay plan checksum differs")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "mutation_request_sha256": self.mutation_request_sha256,
            "mutation_authorization_sha256": self.mutation_authorization_sha256,
            "steps": [item.to_mapping() for item in self.steps],
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "replay_plan_sha256": self.replay_plan_sha256,
        }

    @classmethod
    def create(
        cls,
        *,
        mutation_request_sha256: str,
        mutation_authorization_sha256: str,
        steps: tuple[BatchProjectOperationReplayStep, ...],
    ) -> "BatchProjectOperationReplayPlan":
        values = {
            "schema_name": "mif-batch-project-operation-replay-plan",
            "schema_version": 1,
            "mutation_request_sha256": mutation_request_sha256,
            "mutation_authorization_sha256": mutation_authorization_sha256,
            "steps": tuple(steps),
        }
        material = {
            **values,
            "steps": [item.to_mapping() for item in steps],
        }
        return cls(**values, replay_plan_sha256=_digest(material))  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "BatchProjectOperationReplayPlan":
        material = dict(
            _closed(
                value,
                frozenset(cls.__dataclass_fields__),
                "operation replay plan",
            )
        )
        raw_steps = material["steps"]
        if not isinstance(raw_steps, list):
            raise BatchProjectError("operation replay steps must be an array")
        material["steps"] = tuple(
            BatchProjectOperationReplayStep.from_mapping(item) for item in raw_steps
        )
        return cls(**material)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class BatchProjectOperationCheckpoint:
    schema_name: Literal["mif-batch-project-operation-checkpoint"]
    schema_version: Literal[1]
    replay_plan_sha256: str
    mutation_request_sha256: str
    ordinal: int
    role: Literal["identity_materialization", "primary_mif", "moving_reference"]
    operation: Literal["ensure_project_identity", "upsert_entry"]
    request_identity_sha256: str
    request_envelope_sha256: str
    request_payload_sha256: str
    request_byte_count: int
    evidence_sha256: str
    evidence_payload_sha256: str
    evidence_byte_count: int
    predecessor_checkpoint_sha256: str | None
    checkpoint_sha256: str

    def __post_init__(self) -> None:
        """Validate the BatchProjectOperationCheckpoint fields when the record is constructed.

        The checks cover ``operation``, ``predecessor_checkpoint_sha256``, ``schema_name``,
        ``schema_version``, ``ordinal``, ``role``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        if self.schema_name != "mif-batch-project-operation-checkpoint" or self.schema_version != 1:
            raise BatchProjectError("operation checkpoint schema is unsupported")
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise BatchProjectError("operation checkpoint ordinal is invalid")
        expected_operation = (
            "ensure_project_identity" if self.role == "identity_materialization" else "upsert_entry"
        )
        if self.operation != expected_operation:
            raise BatchProjectError("operation checkpoint role/operation differs")
        for name in (
            "replay_plan_sha256",
            "mutation_request_sha256",
            "request_identity_sha256",
            "request_envelope_sha256",
            "request_payload_sha256",
            "evidence_sha256",
            "evidence_payload_sha256",
        ):
            _sha(getattr(self, name), name)
        _sha(
            self.predecessor_checkpoint_sha256,
            "predecessor_checkpoint_sha256",
            optional=True,
        )
        for name in ("request_byte_count", "evidence_byte_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise BatchProjectError(f"{name} must be a positive integer")
        if _sha(self.checkpoint_sha256, "checkpoint_sha256") != _digest(self._unsigned_mapping()):
            raise BatchProjectError("operation checkpoint checksum differs")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name != "checkpoint_sha256"
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "checkpoint_sha256": self.checkpoint_sha256,
        }

    @classmethod
    def create(cls, **values: object) -> "BatchProjectOperationCheckpoint":
        material = {
            "schema_name": "mif-batch-project-operation-checkpoint",
            "schema_version": 1,
            **values,
        }
        return cls(
            **material,
            checkpoint_sha256=_digest(material),
        )  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "BatchProjectOperationCheckpoint":
        return cls(
            **dict(
                _closed(
                    value,
                    frozenset(cls.__dataclass_fields__),
                    "operation checkpoint",
                )
            )
        )  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class PreparedBatchEntries:
    catalogue: BatchProjectCatalogue
    primary_entry: BatchProjectEntry
    moving_reference_entry: BatchProjectEntry | None
    post_save_project_snapshot: QuPathProjectSnapshot
    runtime_evidence: tuple[
        tuple[QuPathCatalogueRequestEnvelope, QuPathCatalogueEvidenceEnvelope],
        ...,
    ]
    primary_project_entry_artifact_ref: BatchArtifactRef
    raw_outcome: BatchRawNodeOutcome


def _guarded_catalogue_operation(
    method: Callable[..., object],
) -> Callable[..., object]:
    """Retain layout lifetime and the exact issuing writer lease."""

    @wraps(method)
    def guarded(self: "BatchProjectCatalogueStore", *args: object, **kwargs: object) -> object:
        try:
            token = self._runtime_lifecycle.acquire("batch-catalogue-operation")
        except RuntimeLifecycleError as exc:
            raise BatchProjectError("catalogue runtime lifecycle is not open") from exc
        try:
            try:
                with self._lease_use_factory():
                    return method(self, *args, **kwargs)
            except BatchProjectError:
                raise
            except Exception as exc:
                raise BatchProjectError(
                    "catalogue operation requires its issuing live workspace lease"
                ) from exc
        finally:
            token.release()

    setattr(guarded, "_runtime_guard_kind", "catalogue-and-lease")
    return guarded


_BATCH_PROJECT_CATALOGUE_IMPORT_ISSUER = object()


class BatchProjectCatalogueImportAuthority:
    """Opaque one-shot authority for one authenticated capsule catalogue."""

    __slots__ = ("_store", "_issuance_nonce")

    def __new__(cls, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise TypeError("catalogue import authority must be issued by its catalogue owner")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("catalogue import authority cannot be subclassed")

    def __copy__(self) -> object:
        raise TypeError("catalogue import authority is nonserializable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("catalogue import authority is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("catalogue import authority is nonserializable")

    @classmethod
    def _issue(
        cls,
        *,
        issuer: object,
        store: "BatchProjectCatalogueStore",
        issuance_nonce: object,
    ) -> "BatchProjectCatalogueImportAuthority":
        if issuer is not _BATCH_PROJECT_CATALOGUE_IMPORT_ISSUER:
            raise BatchProjectError("catalogue import authority issuer is foreign")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_store", store)
        object.__setattr__(instance, "_issuance_nonce", issuance_nonce)
        return instance


@dataclass(frozen=True, slots=True)
class _BatchProjectCatalogueImportIssuance:
    authority: BatchProjectCatalogueImportAuthority
    issuance_nonce: object
    workspace_lease: object
    project_import_lease: ProjectMutationLease
    request_capability: ProjectMutationRequestCapability
    catalogue: BatchProjectCatalogue
    catalogue_payload_sha256: str
    authenticated_graph_sha256: str
    authority_layout_sha256: str | None
    workspace_storage_scope_sha256: str | None
    generation_ref: AuthorityMemberRefV2
    current_ref: AuthorityMemberRefV2


def _read_current_project_catalogue(
    *,
    read_record: Callable[[str | None], bytes | None],
) -> BatchProjectCatalogue | None:
    """Read both durable records and decode their identical canonical bytes once.

    The current record supplies the validated catalogue and its generation key.
    Exact byte equality then binds that value to the immutable generation without
    constructing a second copy of the same catalogue and every nested entry.
    """

    try:
        payload = read_record(None)
        if payload is None:
            return None
        mapping = json.loads(payload.decode("utf-8", errors="strict"))
        if _canonical_bytes(mapping) != payload:
            raise BatchProjectError("catalogue current pointer is not canonical")
        catalogue = BatchProjectCatalogue.from_mapping(mapping)
        generation_payload = read_record(catalogue.catalogue_sha256)
        if generation_payload is None:
            raise BatchProjectError("catalogue immutable generation is unavailable")
    except (DurableRecordError, UnicodeError, ValueError) as exc:
        raise BatchProjectError("catalogue current pointer is invalid") from exc
    if payload != generation_payload:
        raise BatchProjectError("catalogue current is not its exact immutable-generation copy")
    return catalogue


class BatchProjectCatalogueStore:
    """Workspace-local immutable generations guarded by an issued capability."""

    def __init__(
        self,
        root: Path,
        capability: object,
        validator: Callable[[object], None],
        registered_store_issuer: object,
        marker: object,
        *,
        authority_layout: RetainedAuthorityLayoutV2,
        workspace_storage_scope_sha256: str,
        runtime_lifecycle: RuntimeLifecycleBorrower,
        runtime_binding: RuntimeCompositionBinding,
        lease_use_factory: Callable[[], ContextManager[object]],
    ) -> None:
        if marker is not _STORE_MARKER:
            raise BatchProjectError("catalogue store must be issued by its state authority")
        self._root = Path(root).absolute()
        self._capability = capability
        self._validator = validator
        self._registered_store_issuer = registered_store_issuer
        if not isinstance(runtime_lifecycle, RuntimeLifecycleBorrower):
            raise BatchProjectError("catalogue runtime lifecycle is invalid")
        self._runtime_lifecycle = runtime_lifecycle
        self._runtime_binding = runtime_binding
        self._lease_use_factory = lease_use_factory
        self._bound_runner: object | None = None
        self._bound_operations: QuPathRunnerProjectOperations | None = None
        self._binding_lock = RLock()
        if not isinstance(authority_layout, RetainedAuthorityLayoutV2):
            raise BatchProjectError("catalogue retained authority is invalid")
        self._authority_layout = authority_layout
        self._authority_layout_sha256 = authority_layout.layout.layout_sha256
        self._workspace_storage_scope_sha256 = _sha(
            workspace_storage_scope_sha256, "workspace_storage_scope_sha256",
        )
        try:
            runtime_lifecycle.authenticate_authority_binding(
                runtime_binding,
                authority_layout=authority_layout,
                workspace_storage_scope_sha256=self.workspace_storage_scope_sha256,
            )
        except RuntimeLifecycleError as exc:
            raise BatchProjectError("catalogue runtime binding is invalid") from exc
        authority_layout.validate()
        self._issued_request_capabilities: dict[int, ProjectMutationRequestCapability] = {}
        self._issued_request_refs: dict[int, AuthorityMemberRefV2] = {}
        self._issued_request_kinds: dict[
            int,
            Literal[
                "catalogue_upsert",
                "project_stage",
                "annotation_classifier",
                "registration_project",
            ],
        ] = {}
        self._issued_capsule_import_authorities: dict[int, _BatchProjectCatalogueImportIssuance] = (
            {}
        )
        self._consumed_capsule_import_authorities: dict[
            int, BatchProjectCatalogueImportAuthority
        ] = {}
        self._issued_project_stage_exchanges: dict[
            str, tuple[object, str, object, dict[str, object]]
        ] = {}
        self._known_runtime_requests: dict[str, dict[str, QuPathCatalogueRequestEnvelope]] = {}
        self._known_runtime_evidence: dict[str, dict[str, QuPathCatalogueEvidenceEnvelope]] = {}
        self._retired_member_keys: set[tuple[str, str]] = set()

    @classmethod
    def _issue(
        cls,
        root: Path,
        capability: object,
        validator: Callable[[object], None],
        registered_store_issuer: object,
        *,
        authority_layout: RetainedAuthorityLayoutV2,
        workspace_storage_scope_sha256: str,
        runtime_lifecycle: RuntimeLifecycleBorrower,
        runtime_binding: RuntimeCompositionBinding,
        _lease_use_factory: Callable[[], ContextManager[object]],
    ) -> "BatchProjectCatalogueStore":
        return cls(
            root,
            capability,
            validator,
            registered_store_issuer,
            _STORE_MARKER,
            authority_layout=authority_layout,
            workspace_storage_scope_sha256=workspace_storage_scope_sha256,
            runtime_lifecycle=runtime_lifecycle,
            runtime_binding=runtime_binding,
            lease_use_factory=_lease_use_factory,
        )

    @property
    def root(self) -> Path:
        """Return the legacy workspace locator; it is never retained authority."""

        return self._root

    @property
    def authority_layout_sha256(self) -> str:
        return self._authority_layout_sha256

    @property
    def workspace_storage_scope_sha256(self) -> str:
        return self._workspace_storage_scope_sha256

    @property
    @_guarded_catalogue_operation
    def current_ref(self) -> AuthorityMemberRefV2:
        return self._ref("batch_project_current")

    @property
    def is_quiescent(self) -> bool:
        return self._runtime_lifecycle.is_kind_quiescent("batch-catalogue-operation")

    @_guarded_catalogue_operation
    def bind_project_operations(self, *, runner: object) -> QuPathRunnerProjectOperations:
        """Construct the one exact guarded operations facade without layout escape."""

        with self._binding_lock:
            if self._bound_runner is not None and self._bound_runner is not runner:
                raise BatchProjectError("catalogue runtime runner cannot be replaced")
            if self._bound_operations is not None:
                return self._bound_operations
            binding = self._runtime_binding
            operations = QuPathRunnerProjectOperations(
                runner,
                authority_layout=self._authority_layout,
                workspace_storage_scope_sha256=(self.workspace_storage_scope_sha256),
                runtime_lifecycle=self._runtime_lifecycle,
                runtime_binding=binding,
            )
            self._bound_runner = runner
            self._bound_operations = operations
            return operations

    def _ref(self, family: str, **natural_key: object) -> AuthorityMemberRefV2:
        scope: dict[str, object] = {
            "workspace_storage_scope_sha256": self.workspace_storage_scope_sha256
        }
        if family in {
            "batch_project_replay_plan",
            "batch_project_checkpoint",
            "batch_project_runtime_replay",
        }:
            scope["mutation_request_sha256"] = natural_key.pop("mutation_request_sha256")
        try:
            return authority_member_ref_v2(
                self._authority_layout.layout,
                family=family,
                scope=scope,
                natural_key=natural_key,
            )
        except (KeyError, DurableRecordError) as exc:
            raise BatchProjectError("catalogue authority member key is invalid") from exc

    @_guarded_catalogue_operation
    def generation_ref(self, catalogue_sha256: str) -> AuthorityMemberRefV2:
        return self._ref(
            "batch_project_generation",
            catalogue_sha256=_sha(catalogue_sha256, "catalogue_sha256"),
        )

    def _generation_locator(self, catalogue_sha256: str) -> Path:
        digest = _sha(catalogue_sha256, "catalogue_sha256")
        ref = self.generation_ref(digest)
        bucket = self._bucket_for_ref(ref)
        try:
            locator = bucket.member_path(ref)
        except DurableRecordError as exc:
            raise BatchProjectError("catalogue generation locator is unavailable") from exc
        if not isinstance(locator, Path) or not locator.is_absolute():
            raise BatchProjectError("catalogue generation locator is invalid")
        return locator

    @_guarded_catalogue_operation
    def request_ref(self, request_sha256: str) -> AuthorityMemberRefV2:
        return self._ref(
            "batch_project_request",
            request_sha256=_sha(request_sha256, "request_sha256"),
        )

    @_guarded_catalogue_operation
    def runtime_evidence_ref(self, evidence_sha256: str) -> AuthorityMemberRefV2:
        return self._ref(
            "batch_project_runtime_evidence",
            evidence_sha256=_sha(evidence_sha256, "evidence_sha256"),
        )

    @_guarded_catalogue_operation
    def replay_plan_ref(self, mutation_request_sha256: str) -> AuthorityMemberRefV2:
        return self._ref(
            "batch_project_replay_plan",
            mutation_request_sha256=_sha(mutation_request_sha256, "mutation_request_sha256"),
        )

    @_guarded_catalogue_operation
    def checkpoint_ref(self, mutation_request_sha256: str, ordinal: int) -> AuthorityMemberRefV2:
        return self._ref(
            "batch_project_checkpoint",
            mutation_request_sha256=_sha(mutation_request_sha256, "mutation_request_sha256"),
            ordinal=ordinal,
        )

    @_guarded_catalogue_operation
    def runtime_replay_ref(self, mutation_request_sha256: str) -> AuthorityMemberRefV2:
        return self._ref(
            "batch_project_runtime_replay",
            mutation_request_sha256=_sha(mutation_request_sha256, "mutation_request_sha256"),
        )

    def _bucket_for_ref(self, ref: AuthorityMemberRefV2) -> object:
        if ref.authority_layout_sha256 != self.authority_layout_sha256:
            raise BatchProjectError("catalogue member belongs to another authority")
        family = self._authority_layout.layout.family(ref.family)
        return self._authority_layout.bucket(family.physical_bucket)

    def _publish_ref(
        self,
        ref: AuthorityMemberRefV2,
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
    ) -> None:
        try:
            self._authority_layout.layout.family(ref.family).validate_payload(payload)
            self._bucket_for_ref(ref).publish_immutable(
                ref.physical_name,
                payload,
                max_bytes=max_bytes,
                label=label,
            )
            if self._read_ref(ref, max_bytes=max_bytes, label=label) != payload:
                raise BatchProjectError(f"{label} changed after publication")
        except DurableRecordError as exc:
            raise BatchProjectError(f"{label} conflicts") from exc

    def _read_ref(
        self,
        ref: AuthorityMemberRefV2,
        *,
        max_bytes: int,
        label: str,
        optional: bool = False,
    ) -> bytes | None:
        try:
            return self._bucket_for_ref(ref).read_immutable(
                ref.physical_name,
                max_bytes=max_bytes,
                label=label,
                optional=optional,
            )
        except DurableRecordError as exc:
            raise BatchProjectError(f"{label} is unavailable") from exc

    @_guarded_catalogue_operation
    def retirement_ref(
        self,
        retired_ref: AuthorityMemberRefV2,
        *,
        closure_authority_sha256: str,
    ) -> AuthorityMemberRefV2:
        closure = _sha(closure_authority_sha256, "closure_authority_sha256")
        assert closure is not None
        return authority_member_ref_v2(
            self._authority_layout.layout,
            family="member_retirement",
            scope={
                "retired_family": retired_ref.family,
                "retired_key_sha256": retired_ref.key_sha256,
            },
            natural_key={"closure_authority_sha256": closure},
        )

    def _publish_retirement(
        self,
        retired_ref: AuthorityMemberRefV2,
        *,
        retired_payload_sha256: str,
        closure_kind: Literal["successor", "checkpoint", "archive", "abort"],
        closure_authority_sha256: str,
    ) -> AuthorityMemberRefV2:
        retirement = AuthorityMemberRetirementV2.create(
            layout=self._authority_layout.layout,
            retired_member=retired_ref,
            retired_payload_sha256=retired_payload_sha256,
            closure_kind=closure_kind,
            closure_authority_sha256=closure_authority_sha256,
        )
        ref = self.retirement_ref(
            retired_ref,
            closure_authority_sha256=closure_authority_sha256,
        )
        self._publish_ref(
            ref,
            retirement.to_bytes(),
            max_bytes=self._authority_layout.layout.family("member_retirement").max_member_bytes,
            label="catalogue member retirement",
        )
        self.reopen_retirement(ref)
        self._retired_member_keys.add((retired_ref.family, retired_ref.key_sha256))
        return ref

    @_guarded_catalogue_operation
    def reopen_retirement(self, ref: AuthorityMemberRefV2) -> bytes:
        if ref.family != 'member_retirement':
            raise BatchProjectError("catalogue retirement reference is invalid")
        maximum = self._authority_layout.layout.family(ref.family).max_member_bytes
        payload = self._read_ref(ref, max_bytes=maximum, label="catalogue member retirement")
        assert payload is not None
        retirement = AuthorityMemberRetirementV2.from_bytes(payload)
        if retirement.authority_layout_sha256 != self.authority_layout_sha256:
            raise BatchProjectError("catalogue retirement belongs to another layout")
        return payload

    def _authorize(self, capability: object) -> None:
        if capability is not self._capability:
            raise BatchProjectError(
                "catalogue mutation requires the issuing live workspace capability"
            )
        if self._validator is not None:
            try:
                self._validator(capability)
            except Exception as exc:
                raise BatchProjectError(
                    "catalogue mutation requires a live workspace lease"
                ) from exc

    def _request_path(self, request_sha256: str) -> Path:
        digest = _sha(request_sha256, "request_sha256")
        return self._root / "project-requests" / f"{digest}.json"

    def _reopen_request_bytes(self, request_sha256: str) -> bytes:
        payload = self._read_ref(
            self.request_ref(request_sha256),
            max_bytes=_PROJECT_REQUEST_MAX_BYTES,
            label="persisted project request",
        )
        assert payload is not None
        try:
            mapping = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BatchProjectError("persisted project request is invalid") from exc
        if _canonical_bytes(mapping) != payload:
            raise BatchProjectError("persisted project request is not canonical")
        if mapping.get("schema_name") == "mif-batch-project-upsert-request":
            unsigned = {key: item for key, item in mapping.items() if key != "request_sha256"}
            if (
                mapping.get("request_sha256") != request_sha256
                or _digest(unsigned) != request_sha256
            ):
                raise BatchProjectError("persisted upsert request self digest differs")
        elif mapping.get("schema_name") == "mif-qupath-project-designation-request":
            parsed = ProjectDesignationRequest.from_mapping(mapping)
            if parsed.request_sha256 != request_sha256:
                raise BatchProjectError("persisted designation request self digest differs")
        elif mapping.get("schema_name") == "mif-annotation-classifier-project-request":
            parsed = AnnotationClassifierProjectRequest.from_mapping(mapping)
            if parsed.request_sha256 != request_sha256:
                raise BatchProjectError(
                    "persisted annotation-classifier request self digest differs"
                )
        elif mapping.get("schema_name") == "mif-batch-registration-project-request":
            parsed = BatchRegistrationProjectRequest.from_mapping(mapping)
            if parsed.request_sha256 != request_sha256:
                raise BatchProjectError(
                    "persisted registration-project request self digest differs"
                )
        elif mapping.get("schema_name") == "mif-project-stage-run-request":
            from .project_stage_plan import ProjectStageRunRequest

            parsed = ProjectStageRunRequest.from_mapping(mapping)
            if (
                parsed.request_sha256 != request_sha256
                or parsed.authority_layout_sha256 != self.authority_layout_sha256
            ):
                raise BatchProjectError("persisted project-stage request authority differs")
        else:
            raise BatchProjectError("persisted project request schema is unsupported")
        return payload

    def _authenticate_request_capability(
        self,
        capability: ProjectMutationRequestCapability,
        *,
        expected_kind: (
            Literal[
                "catalogue_upsert",
                "project_stage",
                "annotation_classifier",
                "registration_project",
            ]
            | None
        ) = None,
    ) -> Mapping[str, object]:
        if (
            not isinstance(capability, ProjectMutationRequestCapability)
            or self._issued_request_capabilities.get(id(capability)) is not capability
            or capability._store_issuer is not self
        ):
            raise BatchProjectError("project request capability is not store-issued")
        self._authorize(self._capability)
        request_sha256 = _sha(capability.mutation_request_sha256, "mutation_request_sha256")
        authorization_sha256 = _sha(
            capability.mutation_authorization_sha256,
            "mutation_authorization_sha256",
        )
        assert request_sha256 is not None and authorization_sha256 is not None
        expected_path = self._request_path(request_sha256)
        binding = self._workspace_binding(self._capability)
        request_ref = self._issued_request_refs.get(id(capability))
        expected_ref = self.request_ref(request_sha256)
        expected_capability_binding = _digest(
            {
                "request_sha256": request_sha256,
                "mutation_authorization_sha256": authorization_sha256,
                "authority_layout_sha256": self.authority_layout_sha256,
                "workspace_storage_scope_sha256": (self.workspace_storage_scope_sha256),
                "persisted_ref": expected_ref.to_mapping(),
                "workspace_lease_binding_sha256": binding,
            }
        )
        if (
            capability.persisted_path != expected_path
            or request_ref != expected_ref
            or capability.workspace_lease_binding_sha256 != binding
            or capability.mutation_request_capability_binding_sha256 != expected_capability_binding
            or getattr(capability._runtime_authority_reopener, "__self__", None) is not self
            or getattr(capability._runtime_unlaunched_reopener, "__self__", None) is not self
        ):
            raise BatchProjectError("project request capability binding differs")
        payload = self._reopen_request_bytes(request_sha256)
        try:
            mapping = json.loads(payload.decode("utf-8"))
            schema_name = mapping["schema_name"]
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise BatchProjectError("project request kind is unavailable") from exc
        issued_kind = self._issued_request_kinds.get(id(capability))
        retained_kind = (
            "project_stage"
            if schema_name == "mif-project-stage-run-request"
            else (
                "annotation_classifier"
                if schema_name == "mif-annotation-classifier-project-request"
                else (
                    "registration_project"
                    if schema_name == "mif-batch-registration-project-request"
                    else "catalogue_upsert"
                )
            )
        )
        if issued_kind != retained_kind or (
            expected_kind is not None and retained_kind != expected_kind
        ):
            raise BatchProjectError("project request capability kind differs")
        return mapping

    @_guarded_catalogue_operation
    def request_mapping(
        self, capability: ProjectMutationRequestCapability
    ) -> Mapping[str, object]:
        """Read the current retained request through its issued store owner."""
        return self._authenticate_request_capability(capability)

    @_guarded_catalogue_operation
    def retained_materialization(
        self, capability: ProjectMutationRequestCapability
    ) -> ProjectIdentityMaterializationAttestation | None:
        """Reopen identity materialization from this request's checkpoint chain."""
        self._authenticate_request_capability(capability)
        digest = capability.mutation_request_sha256
        if self._read_ref(
            self.replay_plan_ref(digest),
            max_bytes=_OPERATION_REPLAY_PLAN_MAX_BYTES,
            label="operation replay plan",
            optional=True,
        ) is None:
            return None
        plan = self.reopen_operation_replay_plan(digest, workspace_lease=self._capability)
        checkpoints = self.reopen_operation_checkpoints(plan, workspace_lease=self._capability)
        matches = tuple(
            evidence.materialization_attestation
            for _checkpoint, request, evidence in checkpoints
            if request.operation == "ensure_project_identity"
        )
        if len(matches) > 1:
            raise BatchProjectError("operation chain has duplicate identity materializations")
        return matches[0] if matches else None

    def _validate_capsule_import_binding(
        self,
        catalogue: BatchProjectCatalogue,
        *,
        authenticated_graph_sha256: str,
        request_capability: ProjectMutationRequestCapability,
        project_import_lease: ProjectMutationLease,
        workspace_lease: object,
    ) -> tuple[AuthorityMemberRefV2, AuthorityMemberRefV2, str]:
        """Authenticate the exact live owners of one imported snapshot.

        Structured inputs are decoded through their typed ``from_mapping`` constructors. The
        remaining comparisons check the relationships required by this operation before the
        caller can use those records."""

        self._authorize(workspace_lease)
        if not isinstance(catalogue, BatchProjectCatalogue):
            raise BatchProjectError("capsule catalogue must be typed")
        graph_sha256 = _sha(authenticated_graph_sha256, "authenticated_graph_sha256")
        assert graph_sha256 is not None
        self._authenticate_request_capability(request_capability)
        if (
            not isinstance(project_import_lease, ProjectMutationLease)
            or not project_import_lease.is_live
            or project_import_lease.operation != "capsule_import"
            or not isinstance(
                project_import_lease._acquisition_session,
                ProjectMutationAcquisitionSession,
            )
            or project_import_lease._acquisition_session._mutation_request_capability
            is not request_capability
            or request_capability._store_issuer is not self
        ):
            raise BatchProjectError(
                "capsule catalogue import requires its live project acquisition"
            )
        try:
            project_import_lease._provider._validate_live_lease_authority(
                project_import_lease,
                require_project=True,
            )
            provider_scope = project_import_lease._provider._workspace_storage_scope_sha256(
                project_import_lease
            )
        except Exception as exc:
            raise BatchProjectError(
                "capsule catalogue import project lease is stale or foreign"
            ) from exc
        if (
            project_import_lease.project_authority.authority_layout_sha256
            != self.authority_layout_sha256
            or provider_scope != self.workspace_storage_scope_sha256
        ):
            raise BatchProjectError("capsule catalogue import retained authority differs")
        session = project_import_lease._acquisition_session
        record = session.record
        inventory = project_import_lease._provider_inventory
        if (
            not isinstance(inventory, ProjectMutationArtifactInventory)
            or inventory is not session._artifact_inventory_capability.inventory
            or record.operation != "capsule_import"
            or record.acquisition_record_sha256 != project_import_lease.acquisition_record_sha256
            or record.artifact_inventory_sha256 != inventory.artifact_inventory_sha256
            or record.mutation_request_sha256 != request_capability.mutation_request_sha256
            or record.mutation_authorization_sha256
            != request_capability.mutation_authorization_sha256
            or inventory.mutation_request_sha256 != request_capability.mutation_request_sha256
            or inventory.mutation_authorization_sha256
            != request_capability.mutation_authorization_sha256
            or inventory.expected_project_identity_sha256 != catalogue.project_identity_sha256
            or record.expected_project_identity_sha256 != catalogue.project_identity_sha256
            or record.project_uri != catalogue.project_uri
            or project_import_lease.designation.project_uri != catalogue.project_uri
            or project_import_lease.designation.identity.project_identity_sha256
            != catalogue.project_identity_sha256
            or project_import_lease._catalogue_candidate_sha256 != catalogue.catalogue_sha256
        ):
            raise BatchProjectError("capsule catalogue import acquisition graph differs")
        request = ProjectDesignationRequest.from_mapping(
            json.loads(
                self._reopen_request_bytes(request_capability.mutation_request_sha256).decode(
                    "utf-8"
                )
            )
        )
        if (
            request.request_sha256 != request_capability.mutation_request_sha256
            or request.authorizing_record_sha256 != request_capability.mutation_authorization_sha256
            or request.batch_id != catalogue.batch_id
            or request.project_uri != catalogue.project_uri
            or request.expected_project_identity_sha256 != catalogue.project_identity_sha256
            or request.expected_authority_head_sha256 != record.expected_authority_head_sha256
        ):
            raise BatchProjectError("capsule catalogue import designation differs")
        payload = _canonical_bytes(catalogue.to_mapping())
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        expected_roles = {
            "catalogue_generation": ("capsule_catalogue_generation/" + catalogue.catalogue_sha256),
            "catalogue_current": ("capsule_catalogue_current/" + catalogue.catalogue_sha256),
        }
        catalogue_targets = tuple(
            target for target in inventory.static_targets
            if target.target_kind in expected_roles
            and target.relative_locator_or_metadata_key == expected_roles[target.target_kind]
        )
        if (
            len(catalogue_targets) != 2
            or {target.target_kind for target in catalogue_targets} != set(expected_roles)
            or any(
                target.resolution_kind != "authority_attestation_from_verified_evidence"
                or target.relative_locator_or_metadata_key != expected_roles[target.target_kind]
                or target.predecessor_sha256 != payload_sha256
                for target in catalogue_targets
            )
        ):
            raise BatchProjectError("capsule catalogue import inventory differs")
        generation_ref = self.generation_ref(catalogue.catalogue_sha256)
        current_ref = self.current_ref
        assert current_ref is not None
        return generation_ref, current_ref, payload_sha256

    @_guarded_catalogue_operation
    def prepare_capsule_import_current(
        self,
        catalogue: BatchProjectCatalogue,
        *,
        authenticated_graph_sha256: str,
        request_capability: ProjectMutationRequestCapability,
        project_import_lease: ProjectMutationLease,
        workspace_lease: object,
    ) -> BatchProjectCatalogueImportAuthority:
        """Prepare one exact v2 generation/current import after authentication."""

        generation_ref, current_ref, payload_sha256 = self._validate_capsule_import_binding(
            catalogue,
            authenticated_graph_sha256=authenticated_graph_sha256,
            request_capability=request_capability,
            project_import_lease=project_import_lease,
            workspace_lease=workspace_lease,
        )
        current = self.load_current()
        if (
            current is not None
            and current != catalogue
            and current.catalogue_sha256 != catalogue.predecessor_catalogue_sha256
        ):
            raise BatchProjectCatalogueConflictError(
                "capsule catalogue current is not absent, exact, or its named predecessor"
            )
        issuance_nonce = object()
        authority = BatchProjectCatalogueImportAuthority._issue(
            issuer=_BATCH_PROJECT_CATALOGUE_IMPORT_ISSUER,
            store=self,
            issuance_nonce=issuance_nonce,
        )
        issuance = _BatchProjectCatalogueImportIssuance(
            authority=authority,
            issuance_nonce=issuance_nonce,
            workspace_lease=workspace_lease,
            project_import_lease=project_import_lease,
            request_capability=request_capability,
            catalogue=catalogue,
            catalogue_payload_sha256=payload_sha256,
            authenticated_graph_sha256=authenticated_graph_sha256,
            authority_layout_sha256=self.authority_layout_sha256,
            workspace_storage_scope_sha256=self.workspace_storage_scope_sha256,
            generation_ref=generation_ref,
            current_ref=current_ref,
        )
        self._issued_capsule_import_authorities[id(authority)] = issuance
        return authority

    @_guarded_catalogue_operation
    def commit_capsule_import_current(
        self,
        authority: BatchProjectCatalogueImportAuthority,
        *,
        workspace_lease: object,
    ) -> BatchProjectCatalogue:
        """Consume one prepared import authority and publish generation/current."""

        issued = self._require_capsule_import_issuance(authority, workspace_lease=workspace_lease)
        generation_ref, current_ref, payload_sha256 = self._validate_capsule_import_binding(
            issued.catalogue,
            authenticated_graph_sha256=(issued.authenticated_graph_sha256),
            request_capability=issued.request_capability,
            project_import_lease=issued.project_import_lease,
            workspace_lease=workspace_lease,
        )
        if (
            generation_ref != issued.generation_ref
            or current_ref != issued.current_ref
            or payload_sha256 != issued.catalogue_payload_sha256
        ):
            raise BatchProjectError("capsule catalogue import authority changed")
        current = self.load_current()
        if current == issued.catalogue:
            self._consume_capsule_import_authority(authority)
            return issued.catalogue
        if (
            current is not None
            and current.catalogue_sha256 != issued.catalogue.predecessor_catalogue_sha256
        ):
            raise BatchProjectCatalogueConflictError("capsule catalogue current changed")
        self._consume_capsule_import_authority(authority)
        payload = _canonical_bytes(issued.catalogue.to_mapping())
        self._publish_ref(
            generation_ref,
            payload,
            max_bytes=_CATALOGUE_RECORD_MAX_BYTES,
            label="capsule catalogue immutable generation",
        )
        current_bucket = self._bucket_for_ref(current_ref)
        generation_bucket = self._bucket_for_ref(generation_ref)
        try:
            current_bucket.replace_pointer_cas(
                current_ref.physical_name,
                generation_bucket,
                generation_ref.physical_name,
                expected_predecessor_bytes=(
                    None if current is None else _canonical_bytes(current.to_mapping())
                ),
                max_bytes=_CATALOGUE_RECORD_MAX_BYTES,
                label="capsule catalogue current pointer",
            )
        except DurableRecordError as exc:
            raise BatchProjectCatalogueConflictError("capsule catalogue current changed") from exc
        if self.load_current() != issued.catalogue:
            raise BatchProjectError("capsule catalogue current changed after publication")
        return issued.catalogue

    def _require_capsule_import_issuance(
        self,
        authority: BatchProjectCatalogueImportAuthority,
        *,
        workspace_lease: object,
    ) -> _BatchProjectCatalogueImportIssuance:
        """Authenticate one exact store-issued, still-live import capability."""

        if type(authority) is not BatchProjectCatalogueImportAuthority:
            raise BatchProjectError("capsule catalogue import authority is foreign")
        issued = self._issued_capsule_import_authorities.get(id(authority))
        if issued is None:
            if self._consumed_capsule_import_authorities.get(id(authority)) is authority:
                raise BatchProjectError("capsule catalogue import authority is already consumed")
            raise BatchProjectError("capsule catalogue import authority is foreign")
        if (
            issued.authority is not authority
            or authority._store is not self
            or authority._issuance_nonce is not issued.issuance_nonce
            or issued.workspace_lease is not workspace_lease
            or issued.authority_layout_sha256 != self.authority_layout_sha256
            or issued.workspace_storage_scope_sha256 != self.workspace_storage_scope_sha256
        ):
            raise BatchProjectError("capsule catalogue import authority is foreign")
        return issued

    @_guarded_catalogue_operation
    def authenticate_capsule_import_authority(
        self,
        authority: BatchProjectCatalogueImportAuthority,
        *,
        authenticated_graph_sha256: str,
        catalogue: BatchProjectCatalogue,
        workspace_lease: object,
    ) -> None:
        """Authenticate the immutable issuance snapshot without consuming it."""

        issued = self._require_capsule_import_issuance(authority, workspace_lease=workspace_lease)
        if (
            issued.authenticated_graph_sha256
            != _sha(authenticated_graph_sha256, "authenticated_graph_sha256")
            or issued.catalogue != catalogue
        ):
            raise BatchProjectError("capsule catalogue import authority changed")

    def _consume_capsule_import_authority(
        self, authority: BatchProjectCatalogueImportAuthority
    ) -> None:
        self._issued_capsule_import_authorities.pop(id(authority))
        self._consumed_capsule_import_authorities[id(authority)] = authority

    @_guarded_catalogue_operation
    def load_current(self) -> BatchProjectCatalogue | None:
        def read_record(digest: str | None) -> bytes | None:
            ref = self.current_ref if digest is None else self.generation_ref(digest)
            assert ref is not None
            return self._read_ref(
                ref,
                max_bytes=_CATALOGUE_RECORD_MAX_BYTES,
                label=(
                    "catalogue current pointer"
                    if digest is None
                    else "catalogue immutable generation"
                ),
                optional=digest is None,
            )

        return _read_current_project_catalogue(read_record=read_record)

    @_guarded_catalogue_operation
    def load_generation(self, catalogue_sha256: str) -> BatchProjectCatalogue:
        """Read generation into its typed representation.

        The decoded fields are checked through the record constructors and the applicable
        context comparisons before returning the result. The validated representation is
        returned for the subsequent operation."""

        digest = _sha(catalogue_sha256, "catalogue_sha256")
        assert digest is not None
        payload = self._read_ref(
            self.generation_ref(digest),
            max_bytes=_CATALOGUE_RECORD_MAX_BYTES,
            label="catalogue immutable generation",
        )
        assert payload is not None
        try:
            mapping = json.loads(payload.decode("utf-8", errors="strict"))
            if not isinstance(mapping, Mapping) or _canonical_bytes(mapping) != payload:
                raise BatchProjectError("catalogue immutable generation is not canonical")
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BatchProjectError("catalogue immutable generation is invalid") from exc
        catalogue = BatchProjectCatalogue.from_mapping(mapping)
        if catalogue.catalogue_sha256 != digest:
            raise BatchProjectError("catalogue immutable generation identity differs")
        return catalogue

    @_guarded_catalogue_operation
    def has_operation_replay_plan(self, mutation_request_sha256: str) -> bool:
        return (
            self._read_ref(
                self.replay_plan_ref(mutation_request_sha256),
                max_bytes=_OPERATION_REPLAY_PLAN_MAX_BYTES,
                label="operation replay plan",
                optional=True,
            )
            is not None
        )

    @_guarded_catalogue_operation
    def has_runtime_replay(self, mutation_request_sha256: str) -> bool:
        return (
            self._read_ref(
                self.runtime_replay_ref(mutation_request_sha256),
                max_bytes=_RUNTIME_REPLAY_MAX_BYTES,
                label="runtime replay record",
                optional=True,
            )
            is not None
        )

    @staticmethod
    def _workspace_binding(workspace_lease: object) -> str:
        try:
            return _digest(
                {
                    "workspace_root": workspace_lease.workspace_root.as_posix(),
                    "operation": workspace_lease.operation,
                    "fencing_token": workspace_lease.fencing_token,
                }
            )
        except (AttributeError, TypeError) as exc:
            raise BatchProjectError("workspace lease cannot be bound") from exc

    @_guarded_catalogue_operation
    def publish_request(
        self,
        request: EnsureBatchEntriesRequest | ProjectDesignationRequest,
        *,
        mutation_authorization_sha256: str,
        workspace_lease: object,
    ) -> ProjectMutationRequestCapability:
        self._authorize(workspace_lease)
        if not isinstance(request, (EnsureBatchEntriesRequest, ProjectDesignationRequest)):
            raise BatchProjectError("project request must use a current typed schema")
        authorization = _sha(mutation_authorization_sha256, "mutation_authorization_sha256")
        assert authorization is not None
        path = self._root / "project-requests" / f"{request.request_sha256}.json"
        payload = _canonical_bytes(request.to_mapping())
        request_ref = self.request_ref(request.request_sha256)
        self._publish_ref(
            request_ref,
            payload,
            max_bytes=_PROJECT_REQUEST_MAX_BYTES,
            label="immutable catalogue request",
        )
        binding = self._workspace_binding(workspace_lease)
        capability_binding = _digest(
            {
                "request_sha256": request.request_sha256,
                "mutation_authorization_sha256": authorization,
                **(
                    {
                        "authority_layout_sha256": self.authority_layout_sha256,
                        "workspace_storage_scope_sha256": (self.workspace_storage_scope_sha256),
                        "persisted_ref": request_ref.to_mapping(),
                    }
                    if request_ref is not None
                    else {"persisted_path": path.as_posix()}
                ),
                "workspace_lease_binding_sha256": binding,
            }
        )
        capability = ProjectMutationRequestCapability._issue(
            mutation_request_sha256=request.request_sha256,
            mutation_authorization_sha256=authorization,
            persisted_path=path,
            workspace_lease_binding_sha256=binding,
            mutation_request_capability_binding_sha256=capability_binding,
            _runtime_authority_reopener=(self._reopen_provider_runtime_authority),
            _runtime_unlaunched_reopener=(self._reopen_provider_unlaunched_authority),
            _store_issuer=self,
        )
        self._issued_request_capabilities[id(capability)] = capability
        self._issued_request_kinds[id(capability)] = "catalogue_upsert"
        if request_ref is not None:
            self._issued_request_refs[id(capability)] = request_ref
        return capability

    @_guarded_catalogue_operation
    def _publish_annotation_classifier_request(
        self,
        request: AnnotationClassifierProjectRequest,
        *,
        mutation_authorization_sha256: str,
        workspace_lease: object,
        state_store_issuer: object,
    ) -> ProjectMutationRequestCapability:
        """Issue the distinct classifier request under annotation-fit semantics."""

        self._authorize(workspace_lease)
        if (
            state_store_issuer is not self._registered_store_issuer
            or type(request) is not AnnotationClassifierProjectRequest
            or request.intent != "open_existing"
        ):
            raise BatchProjectError("annotation-classifier request requires its state-store issuer")
        authorization = _sha(mutation_authorization_sha256, "mutation_authorization_sha256")
        assert authorization is not None
        path = self._root / "project-requests" / f"{request.request_sha256}.json"
        payload = _canonical_bytes(request.to_mapping())
        request_ref = self.request_ref(request.request_sha256)
        self._publish_ref(
            request_ref,
            payload,
            max_bytes=_PROJECT_REQUEST_MAX_BYTES,
            label="immutable annotation-classifier request",
        )
        binding = self._workspace_binding(workspace_lease)
        capability_binding = _digest(
            {
                "request_sha256": request.request_sha256,
                "mutation_authorization_sha256": authorization,
                **(
                    {
                        "authority_layout_sha256": self.authority_layout_sha256,
                        "workspace_storage_scope_sha256": (self.workspace_storage_scope_sha256),
                        "persisted_ref": request_ref.to_mapping(),
                    }
                    if request_ref is not None
                    else {"persisted_path": path.as_posix()}
                ),
                "workspace_lease_binding_sha256": binding,
            }
        )
        capability = ProjectMutationRequestCapability._issue(
            mutation_request_sha256=request.request_sha256,
            mutation_authorization_sha256=authorization,
            persisted_path=path,
            workspace_lease_binding_sha256=binding,
            mutation_request_capability_binding_sha256=capability_binding,
            _runtime_authority_reopener=self._reopen_provider_runtime_authority,
            _runtime_unlaunched_reopener=self._reopen_provider_unlaunched_authority,
            _store_issuer=self,
        )
        self._issued_request_capabilities[id(capability)] = capability
        self._issued_request_kinds[id(capability)] = "annotation_classifier"
        if request_ref is not None:
            self._issued_request_refs[id(capability)] = request_ref
        return capability

    def _issue_reopened_request_capability(
        self,
        *,
        request_sha256: str,
        mutation_authorization_sha256: str,
        workspace_lease: object,
        kind: Literal[
            "catalogue_upsert",
            "annotation_classifier",
            "registration_project",
        ],
    ) -> ProjectMutationRequestCapability:
        path = self._request_path(request_sha256)
        authorization = _sha(mutation_authorization_sha256, "mutation_authorization_sha256")
        binding = self._workspace_binding(workspace_lease)
        request_ref = (
            self.request_ref(request_sha256)
        )
        capability_binding = _digest(
            {
                "request_sha256": request_sha256,
                "mutation_authorization_sha256": authorization,
                **(
                    {
                        "authority_layout_sha256": self.authority_layout_sha256,
                        "workspace_storage_scope_sha256": (self.workspace_storage_scope_sha256),
                        "persisted_ref": request_ref.to_mapping(),
                    }
                    if request_ref is not None
                    else {"persisted_path": path.as_posix()}
                ),
                "workspace_lease_binding_sha256": binding,
            }
        )
        capability = ProjectMutationRequestCapability._issue(
            mutation_request_sha256=request_sha256,
            mutation_authorization_sha256=authorization,
            persisted_path=path,
            workspace_lease_binding_sha256=binding,
            mutation_request_capability_binding_sha256=capability_binding,
            _runtime_authority_reopener=self._reopen_provider_runtime_authority,
            _runtime_unlaunched_reopener=(self._reopen_provider_unlaunched_authority),
            _store_issuer=self,
        )
        self._issued_request_capabilities[id(capability)] = capability
        self._issued_request_kinds[id(capability)] = kind
        if request_ref is not None:
            self._issued_request_refs[id(capability)] = request_ref
        return capability

    @_guarded_catalogue_operation
    def _publish_registration_project_request(
        self,
        request: BatchRegistrationProjectRequest,
        *,
        mutation_authorization_sha256: str,
        workspace_lease: object,
        state_store_issuer: object,
    ) -> ProjectMutationRequestCapability:
        """Persist one exact registration request for the registered state owner."""

        self._authorize(workspace_lease)
        if (
            state_store_issuer is not self._registered_store_issuer
            or type(request) is not BatchRegistrationProjectRequest
            or request.intent != "open_existing"
            or mutation_authorization_sha256 != request.authorizing_record_sha256
        ):
            raise BatchProjectError("registration-project request requires its state-store issuer")
        path = self._request_path(request.request_sha256)
        payload = _canonical_bytes(request.to_mapping())
        request_ref = self.request_ref(request.request_sha256)
        self._publish_ref(
            request_ref,
            payload,
            max_bytes=_PROJECT_REQUEST_MAX_BYTES,
            label="immutable registration-project request",
        )
        capability = self._issue_reopened_request_capability(
            request_sha256=request.request_sha256,
            mutation_authorization_sha256=mutation_authorization_sha256,
            workspace_lease=workspace_lease,
            kind="registration_project",
        )
        if request_ref is not None:
            self._issued_request_refs[id(capability)] = request_ref
        return capability

    @_guarded_catalogue_operation
    def _reopen_registration_project_request(
        self,
        mutation_request_sha256: str,
        *,
        workspace_lease: object,
        state_store_issuer: object,
    ) -> tuple[
        BatchRegistrationProjectRequest,
        ProjectMutationRequestCapability,
    ]:
        """Reopen only the exact state-owned registration request kind."""

        self._authorize(workspace_lease)
        if state_store_issuer is not self._registered_store_issuer:
            raise BatchProjectError("registration-project request requires its state-store issuer")
        request_sha256 = _sha(
            mutation_request_sha256,
            "mutation_request_sha256",
        )
        assert request_sha256 is not None
        payload = self._reopen_request_bytes(request_sha256)
        try:
            request = BatchRegistrationProjectRequest.from_mapping(
                json.loads(payload.decode("utf-8", errors="strict"))
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise BatchProjectError("persisted registration-project request is invalid") from exc
        if request.request_sha256 != request_sha256:
            raise BatchProjectError("persisted registration-project request identity differs")
        capability = self._issue_reopened_request_capability(
            request_sha256=request_sha256,
            mutation_authorization_sha256=request.authorizing_record_sha256,
            workspace_lease=workspace_lease,
            kind="registration_project",
        )
        return request, capability

    @_guarded_catalogue_operation
    def _reopen_annotation_classifier_request(
        self,
        mutation_request_sha256: str,
        *,
        workspace_lease: object,
        state_store_issuer: object,
    ) -> tuple[
        AnnotationClassifierProjectRequest,
        ProjectMutationRequestCapability,
    ]:
        """Reopen only the exact state-owned classifier request kind."""

        self._authorize(workspace_lease)
        if state_store_issuer is not self._registered_store_issuer:
            raise BatchProjectError("annotation-classifier request requires its state-store issuer")
        request_sha256 = _sha(mutation_request_sha256, "mutation_request_sha256")
        assert request_sha256 is not None
        payload = self._reopen_request_bytes(request_sha256)
        try:
            request = AnnotationClassifierProjectRequest.from_mapping(
                json.loads(payload.decode("utf-8", errors="strict"))
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise BatchProjectError("persisted annotation-classifier request is invalid") from exc
        if request.request_sha256 != request_sha256:
            raise BatchProjectError("persisted annotation-classifier request identity differs")
        capability = self._issue_reopened_request_capability(
            request_sha256=request_sha256,
            mutation_authorization_sha256=request.authorizing_record_sha256,
            workspace_lease=workspace_lease,
            kind="annotation_classifier",
        )
        return request, capability

    @_guarded_catalogue_operation
    def reopen_request(
        self,
        mutation_request_sha256: str,
        *,
        mutation_authorization_sha256: str,
        workspace_lease: object,
    ) -> ProjectMutationRequestCapability:
        self._authorize(workspace_lease)
        request_sha256 = _sha(mutation_request_sha256, "mutation_request_sha256")
        assert request_sha256 is not None
        payload = self._reopen_request_bytes(request_sha256)
        try:
            schema_name = json.loads(payload.decode("utf-8", errors="strict"))["schema_name"]
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise BatchProjectError("persisted project request is invalid") from exc
        if schema_name in {
            "mif-annotation-classifier-project-request",
            "mif-batch-registration-project-request",
        }:
            raise BatchProjectError(
                "state-owned project request cannot be reopened as catalogue authority"
            )
        return self._issue_reopened_request_capability(
            request_sha256=request_sha256,
            mutation_authorization_sha256=mutation_authorization_sha256,
            workspace_lease=workspace_lease,
            kind="catalogue_upsert",
        )

    def _issue_project_stage_request_capability(
        self,
        *,
        request_sha256: str,
        mutation_authorization_sha256: str,
        workspace_lease: object,
    ) -> ProjectMutationRequestCapability:
        request_digest = _sha(request_sha256, "request_sha256")
        authorization = _sha(mutation_authorization_sha256, "mutation_authorization_sha256")
        assert request_digest is not None and authorization is not None
        path = self._request_path(request_digest)
        binding = self._workspace_binding(workspace_lease)
        request_ref = (
            self.request_ref(request_digest)
        )
        capability_binding = _digest(
            {
                "request_sha256": request_digest,
                "mutation_authorization_sha256": authorization,
                **(
                    {
                        "authority_layout_sha256": self.authority_layout_sha256,
                        "workspace_storage_scope_sha256": (self.workspace_storage_scope_sha256),
                        "persisted_ref": request_ref.to_mapping(),
                    }
                    if request_ref is not None
                    else {"persisted_path": path.as_posix()}
                ),
                "workspace_lease_binding_sha256": binding,
            }
        )
        capability = ProjectMutationRequestCapability._issue(
            mutation_request_sha256=request_digest,
            mutation_authorization_sha256=authorization,
            persisted_path=path,
            workspace_lease_binding_sha256=binding,
            mutation_request_capability_binding_sha256=capability_binding,
            _runtime_authority_reopener=self._reopen_provider_runtime_authority,
            _runtime_unlaunched_reopener=self._reopen_provider_unlaunched_authority,
            _store_issuer=self,
        )
        self._issued_request_capabilities[id(capability)] = capability
        self._issued_request_kinds[id(capability)] = "project_stage"
        if request_ref is not None:
            self._issued_request_refs[id(capability)] = request_ref
        return capability

    @_guarded_catalogue_operation
    def publish_project_stage_request(
        self,
        request: object,
        *,
        mutation_authorization_sha256: str,
        workspace_lease: object,
    ) -> ProjectMutationRequestCapability:
        """Refuse direct callers; orchestration derives this authority."""

        del request, mutation_authorization_sha256, workspace_lease
        raise BatchProjectError("project-stage request authority requires its state-store issuer")

    def _publish_project_stage_request(
        self,
        request: object,
        *,
        mutation_authorization_sha256: str,
        workspace_lease: object,
        state_store_issuer: object,
    ) -> ProjectMutationRequestCapability:
        """Publish authority only for the exact registered state store."""

        from .project_stage_plan import ProjectStageRunRequest

        self._authorize(workspace_lease)
        if state_store_issuer is not self._registered_store_issuer:
            raise BatchProjectError("project-stage state-store issuer differs")
        if not isinstance(request, ProjectStageRunRequest):
            raise BatchProjectError("project-stage request must use its current typed schema")
        if request.authority_layout_sha256 != self.authority_layout_sha256:
            raise BatchProjectError("project-stage request authority differs")
        payload = _canonical_bytes(request.to_mapping())
        ref = self.request_ref(request.request_sha256)
        self._publish_ref(
            ref,
            payload,
            max_bytes=_PROJECT_REQUEST_MAX_BYTES,
            label="immutable project-stage request",
        )
        capability = self._issue_project_stage_request_capability(
            request_sha256=request.request_sha256,
            mutation_authorization_sha256=mutation_authorization_sha256,
            workspace_lease=workspace_lease,
        )
        self._authenticate_request_capability(capability, expected_kind="project_stage")
        return capability

    @_guarded_catalogue_operation
    def reopen_project_stage_request_value(
        self,
        request_sha256: str,
        *,
        workspace_lease: object,
    ) -> object:
        """Reopen exact typed intent without issuing mutation authority."""

        self._authorize(workspace_lease)
        return self._reopen_project_stage_request_value(request_sha256)

    def _reopen_project_stage_request_value(self, request_sha256: str) -> object:
        """Parse exact retained project-stage intent for an authorized caller."""

        from .project_stage_plan import ProjectStageRunRequest

        request_digest = _sha(request_sha256, "request_sha256")
        payload = self._reopen_request_bytes(request_digest)
        try:
            request = ProjectStageRunRequest.from_mapping(
                json.loads(payload.decode("utf-8", errors="strict"))
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise BatchProjectError("persisted project-stage request is invalid") from exc
        if (
            request.request_sha256 != request_digest
            or request.authority_layout_sha256 != self.authority_layout_sha256
        ):
            raise BatchProjectError("persisted project-stage request authority differs")
        return request

    @_guarded_catalogue_operation
    def reopen_project_stage_request(
        self,
        request_sha256: str,
        *,
        mutation_authorization_sha256: str,
        workspace_lease: object,
    ) -> tuple[object, ProjectMutationRequestCapability]:
        """Refuse direct callers; orchestration reauthenticates this authority."""

        del request_sha256, mutation_authorization_sha256, workspace_lease
        raise BatchProjectError("project-stage request authority requires its state-store issuer")

    def _reopen_project_stage_request(
        self,
        request_sha256: str,
        *,
        mutation_authorization_sha256: str,
        workspace_lease: object,
        state_store_issuer: object,
    ) -> tuple[object, ProjectMutationRequestCapability]:
        """Reopen authority only for the exact registered state store."""

        self._authorize(workspace_lease)
        if state_store_issuer is not self._registered_store_issuer:
            raise BatchProjectError("project-stage state-store issuer differs")
        request = self._reopen_project_stage_request_value(request_sha256)
        capability = self._issue_project_stage_request_capability(
            request_sha256=request.request_sha256,
            mutation_authorization_sha256=mutation_authorization_sha256,
            workspace_lease=workspace_lease,
        )
        self._authenticate_request_capability(capability, expected_kind="project_stage")
        return request, capability

    def _project_stage_exchange_ref(
        self,
        *,
        active_mutation_run_sha256: str,
        request_identity_sha256: str,
        direction: Literal["request", "result", "generation_manifest"],
    ) -> AuthorityMemberRefV2:
        try:
            return authority_member_ref_v2(
                self._authority_layout.layout,
                family="runtime_exchange",
                scope={
                    "workspace_storage_scope_sha256": (self.workspace_storage_scope_sha256),
                    "active_mutation_run_sha256": _sha(
                        active_mutation_run_sha256,
                        "active_mutation_run_sha256",
                    ),
                    "role": "project_stage",
                    "request_identity_sha256": _sha(
                        request_identity_sha256,
                        "request_identity_sha256",
                    ),
                },
                natural_key={"direction": direction},
            )
        except DurableRecordError as exc:
            raise BatchProjectError("project-stage exchange ref is invalid") from exc

    def _publish_project_stage_target_exchange(
        self,
        target_request: object,
        *,
        request_envelope_bytes: bytes,
        active_mutation_run_sha256: str,
    ) -> object:
        from .project_stage_plan import (
            ProjectStageExchangeAttestation,
            ProjectStageTargetRequest,
        )

        if not isinstance(target_request, ProjectStageTargetRequest):
            raise BatchProjectError("project-stage target request must be typed")
        if type(request_envelope_bytes) is not bytes:
            raise BatchProjectError("project-stage target request envelope must be bytes")
        request_mapping = self._validate_project_stage_request_envelope(
            target_request, request_envelope_bytes
        )
        catalogue = self.load_current()
        try:
            entry = catalogue.resolve(target_request.project_entry_id)
            request = request_mapping["request"]
        except (AttributeError, KeyError, TypeError) as exc:
            raise BatchProjectError("project-stage target catalogue authority differs") from exc
        if (
            entry.image_role != "primary_mif"
            or entry.sample_id != target_request.sample_id
            or entry.slide_id != target_request.slide_id
            or request["canonical_image_uri"] != entry.canonical_uri
            or request["canonical_image_sha256"] != entry.image_sha256
        ):
            raise BatchProjectError("project-stage target catalogue authority differs")
        active = _sha(active_mutation_run_sha256, "active_mutation_run_sha256")
        assert active is not None
        request_envelope_sha256 = hashlib.sha256(request_envelope_bytes).hexdigest()
        request_identity = _digest(
            {
                "active_mutation_run_sha256": active,
                "kind": "project_stage_target_exchange/v2",
                "target_request_sha256": target_request.target_request_sha256,
                "request_envelope_sha256": request_envelope_sha256,
            }
        )
        request_ref = self._project_stage_exchange_ref(
            active_mutation_run_sha256=active,
            request_identity_sha256=request_identity,
            direction="request",
        )
        result_ref = self._project_stage_exchange_ref(
            active_mutation_run_sha256=active,
            request_identity_sha256=request_identity,
            direction="result",
        )
        manifest_ref = self._project_stage_exchange_ref(
            active_mutation_run_sha256=active,
            request_identity_sha256=request_identity,
            direction="generation_manifest",
        )
        self._publish_ref(
            request_ref,
            request_envelope_bytes,
            max_bytes=self._authority_layout.layout.family(  # type: ignore[union-attr]
                "runtime_exchange"
            ).max_member_bytes,
            label="project-stage target exchange request",
        )
        attestation = ProjectStageExchangeAttestation.create(
            active_mutation_run_sha256=active,
            target_request=target_request,
            request_envelope_sha256=request_envelope_sha256,
            request_ref=request_ref,
            request_envelope_byte_count=len(request_envelope_bytes),
            result_ref=result_ref,
            generation_manifest_ref=manifest_ref,
        )
        prior = self._issued_project_stage_exchanges.get(attestation.exchange_sha256)
        issued = (attestation, active, target_request, request_mapping)
        if prior is not None and prior != issued:
            raise BatchProjectError("project-stage exchange authority conflicts")
        self._issued_project_stage_exchanges[attestation.exchange_sha256] = issued
        try:
            if self._reopen_project_stage_target_request(attestation) != request_mapping:
                raise BatchProjectError("project-stage target request changed")
        except BaseException:
            if prior is None:
                self._issued_project_stage_exchanges.pop(attestation.exchange_sha256, None)
            raise
        return attestation

    def _authenticate_project_stage_exchange(self, attestation: object) -> None:
        from .project_stage_plan import ProjectStageExchangeAttestation

        if not isinstance(attestation, ProjectStageExchangeAttestation):
            raise BatchProjectError("project-stage exchange authority is invalid")
        issued = self._issued_project_stage_exchanges.get(attestation.exchange_sha256)
        if issued is None or issued[0] != attestation:
            raise BatchProjectError("project-stage exchange authority was not issued")
        active = issued[1]
        expected_identity = _digest(
            {
                "active_mutation_run_sha256": active,
                "kind": "project_stage_target_exchange/v2",
                "target_request_sha256": attestation.target_request_sha256,
                "request_envelope_sha256": (attestation.request_envelope_sha256),
            }
        )
        expected_refs = tuple(
            self._project_stage_exchange_ref(
                active_mutation_run_sha256=active,
                request_identity_sha256=expected_identity,
                direction=direction,
            )
            for direction in ("request", "result", "generation_manifest")
        )
        if (
            attestation.authority_layout_sha256 != self.authority_layout_sha256
            or attestation.request_identity_sha256 != expected_identity
            or (
                attestation.request_ref,
                attestation.result_ref,
                attestation.generation_manifest_ref,
            )
            != expected_refs
        ):
            raise BatchProjectError("project-stage exchange authority differs")

    @staticmethod
    def _validate_project_stage_request_envelope(
        target_request: object, payload: bytes
    ) -> dict[str, object]:
        """Validate project stage request envelope for the current operation.

        The implementation compares the supplied record identities and content digests with
        the operation context. A mismatch is reported at this boundary before the record is
        accepted by its caller."""

        from .project_stage_codecs import _asset_consumes_resolved_config
        from .project_stage_plan import (
            ProjectStageTargetRequest,
            _project_stage_operation_output_dir,
        )
        from mif_pipeline.qupath_runtime.exchange import (
            ExchangeContractError,
            validate_canonical_image_uri,
        )

        if type(target_request) is not ProjectStageTargetRequest:
            raise BatchProjectError("project-stage target request must be typed")
        if type(payload) is not bytes or not payload:
            raise BatchProjectError("project-stage target request envelope is invalid")
        try:
            request_mapping = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BatchProjectError("project-stage target request envelope is invalid") from exc
        if (
            not isinstance(request_mapping, Mapping)
            or any(type(key) is not str for key in request_mapping)
            or set(request_mapping)
            != {
                "schema_version",
                "invocation_id",
                "run_id",
                "stage",
                "slide_id",
                "request",
            }
            or request_mapping["schema_version"] != "roi-stage-request/v2"
            or request_mapping["run_id"] != target_request.pipeline_run_id
            or request_mapping["stage"] != target_request.wire_stage_id
            or request_mapping["slide_id"] != target_request.slide_id
            or not isinstance(request_mapping["request"], Mapping)
            or canonical_json_bytes(request_mapping) != payload
        ):
            raise BatchProjectError("project-stage target request envelope differs")
        request = dict(request_mapping["request"])
        reserved = {
            "project_file",
            "project_entry_id",
            "canonical_image_uri",
            "canonical_image_sha256",
            "script_bundle_sha256",
            "script_asset_path",
            "script_asset_sha256",
            "exchange_version",
        }
        has_resolved_config = "resolved_config" in request
        if (
            not reserved <= set(request)
            or request.get("project_entry_id") != target_request.project_entry_id
            or request.get("exchange_version") != "roi-stage-request/v2"
            or request.get("script_asset_path")
            != target_request.request_codec_id.removeprefix("qupath/scripts/")
            or "config_path" in request
            or (has_resolved_config != ("config_hash" in request))
            or (
                has_resolved_config
                != _asset_consumes_resolved_config(target_request.request_codec_id)
            )
        ):
            raise BatchProjectError("project-stage runtime request bindings differ")
        for name in (
            "canonical_image_sha256",
            "script_bundle_sha256",
            "script_asset_sha256",
        ):
            try:
                _sha(request.get(name), name)
            except BatchProjectError as exc:
                raise BatchProjectError("project-stage runtime request bindings differ") from exc
        try:
            validate_canonical_image_uri(request.get("canonical_image_uri"))
        except ExchangeContractError as exc:
            raise BatchProjectError("project-stage runtime request bindings differ") from exc
        if "producer_output_dirs" in request:
            roots = request.pop("producer_output_dirs")
            if not isinstance(roots, Mapping):
                raise BatchProjectError("project-stage producer directories are invalid")
            for attempt, root in roots.items():
                if not isinstance(attempt, str) or not attempt or attempt != attempt.strip():
                    raise BatchProjectError("project-stage ROI producer attempt is invalid")
                try:
                    _project_stage_operation_output_dir({"operational_output_dir": root}, {})
                except ValueError as exc:
                    raise BatchProjectError("project-stage producer output directory differs") from exc
        if "operational_output_dir" in request:
            # Runtime publication is bound by the exact envelope, independently
            # of the retained scientific body and configuration hashes. Older
            # envelopes did not carry this operational field.
            try:
                _project_stage_operation_output_dir(request, {})
            except ValueError as exc:
                raise BatchProjectError("project-stage operational output directory differs") from exc
            request.pop("operational_output_dir")
        if "resolved_config" in request:
            config = request.pop("resolved_config")
            config_hash = request.pop("config_hash")
            if (
                not isinstance(config, Mapping)
                or hashlib.sha256(canonical_json_bytes(config)).hexdigest()
                != target_request.resolved_config_sha256
                or config_hash != target_request.resolved_config_sha256
            ):
                raise BatchProjectError("project-stage resolved configuration differs")
        for name in reserved:
            request.pop(name)
        body = canonical_json_bytes(request)
        if (
            hashlib.sha256(body).hexdigest() != target_request.scientific_request_body_sha256
            or len(body) != target_request.scientific_request_body_byte_count
        ):
            raise BatchProjectError("project-stage scientific request body differs")
        return dict(request_mapping)

    def _reopen_project_stage_target_request(self, attestation: object) -> object:
        from .project_stage_plan import ProjectStageExchangeAttestation

        if (
            not isinstance(attestation, ProjectStageExchangeAttestation)
            or attestation.authority_layout_sha256 != self.authority_layout_sha256
        ):
            raise BatchProjectError("project-stage exchange authority differs")
        self._authenticate_project_stage_exchange(attestation)
        payload = self._read_ref(
            attestation.request_ref,
            max_bytes=self._authority_layout.layout.family(  # type: ignore[union-attr]
                "runtime_exchange"
            ).max_member_bytes,
            label="project-stage target exchange request",
        )
        assert payload is not None
        if (
            hashlib.sha256(payload).hexdigest() != attestation.request_envelope_sha256
            or len(payload) != attestation.request_envelope_byte_count
        ):
            raise BatchProjectError("project-stage target request payload differs")
        issued = self._issued_project_stage_exchanges.get(attestation.exchange_sha256)
        if issued is None or len(issued) != 4:
            raise BatchProjectError("project-stage exchange authority was not issued")
        target_request = issued[2]
        if target_request.target_request_sha256 != attestation.target_request_sha256:
            raise BatchProjectError("project-stage target request identity differs")
        return self._validate_project_stage_request_envelope(target_request, payload)

    def _authenticate_project_stage_plan_target(self, target: object) -> None:
        from .project_stage_plan import (
            ProjectStageAuthorizedEntry,
            ProjectStageTargetRequest,
        )

        if not isinstance(target, ProjectStageAuthorizedEntry):
            raise BatchProjectError("project-stage plan target is invalid")
        expected_request = ProjectStageTargetRequest.create(
            node_id=target.node_id,
            project_entry_id=target.project_entry_id,
            sample_id=target.sample_id,
            slide_id=target.slide_id,
            pipeline_run_id=target.pipeline_run_id,
            image_role=target.image_role,
            wire_stage_id=target.wire_stage_id,
            terminal_stage_id=target.terminal_stage_id,
            analysis_domain_bindings=target.analysis_domain_bindings,
            resolved_config_sha256=target.resolved_config_sha256,
            prerequisite_artifact_ref_sha256s=(target.prerequisite_artifact_ref_sha256s),
            saved_state_before_sha256=target.saved_state_before_sha256,
            adapter_spec_sha256=target.adapter_spec_sha256,
            request_codec_id=target.request_codec_id,
            request_codec_version=target.request_codec_version,
            scientific_request_body_sha256=(target.scientific_request_body_sha256),
            scientific_request_body_byte_count=(target.scientific_request_body_byte_count),
        )
        if (
            self._issued_project_stage_exchanges.get(
                target.exchange.exchange_sha256, (None, None, None)
            )[2]
            != expected_request
        ):
            raise BatchProjectError("project-stage plan target exchange authority differs")
        envelope = self._reopen_project_stage_target_request(target.exchange)
        if envelope.get("invocation_id") != target.attempt_id:
            raise BatchProjectError("project-stage plan target attempt identity differs")

    def _publish_project_stage_runtime_record(
        self,
        record: object,
        *,
        record_type: type,
        digest_field: str,
        label: str,
    ) -> AuthorityMemberRefV2:
        if type(record) is not record_type:
            raise BatchProjectError(f"{label} must use its current typed schema")
        if getattr(record, 'authority_layout_sha256', None) != self.authority_layout_sha256:
            raise BatchProjectError(f"{label} authority layout differs")
        record_scope = getattr(record, "workspace_storage_scope_sha256", None)
        if record_scope is not None and record_scope != self.workspace_storage_scope_sha256:
            raise BatchProjectError(f"{label} workspace scope differs")
        digest = _sha(getattr(record, digest_field, None), digest_field)
        assert digest is not None
        ref = self.runtime_evidence_ref(digest)
        family = self._authority_layout.layout.family(ref.family)
        self._publish_ref(
            ref,
            _canonical_bytes(record.to_mapping()),
            max_bytes=family.max_member_bytes,
            label=label,
        )
        return ref

    def _reopen_project_stage_runtime_record(
        self,
        ref: AuthorityMemberRefV2,
        *,
        record_type: type,
        digest_field: str,
        expected_digest: str,
        label: str,
    ) -> object:
        digest = _sha(expected_digest, digest_field)
        assert digest is not None
        if ref != self.runtime_evidence_ref(digest):
            raise BatchProjectError(f"{label} retained reference differs")
        payload = self._read_ref(
            ref,
            max_bytes=self._authority_layout.layout.family(
                "batch_project_runtime_evidence"
            ).max_member_bytes,
            label=label,
        )
        assert payload is not None
        try:
            mapping = json.loads(payload.decode("utf-8", errors="strict"))
            if _canonical_bytes(mapping) != payload:
                raise BatchProjectError(f"{label} is not canonical")
            record = record_type.from_mapping(mapping)
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise BatchProjectError(f"{label} is invalid") from exc
        if (
            getattr(record, digest_field, None) != digest
            or getattr(record, "authority_layout_sha256", None) != self.authority_layout_sha256
            or (
                hasattr(record, "workspace_storage_scope_sha256")
                and record.workspace_storage_scope_sha256 != self.workspace_storage_scope_sha256
            )
        ):
            raise BatchProjectError(f"{label} authority differs")
        return record

    @_guarded_catalogue_operation
    def publish_project_stage_run_plan(self, plan: object, *, workspace_lease: object) -> object:
        del plan, workspace_lease
        raise BatchProjectError(
            "project-stage run-plan publication requires the issuing state-store"
        )

    @_guarded_catalogue_operation
    def _publish_project_stage_run_plan(
        self,
        plan: object,
        *,
        workspace_lease: object,
        state_store_issuer: object,
    ) -> object:
        from .project_stage_plan import (
            ProjectStageRunPlan,
            PublishedProjectStageRunPlan,
        )

        if state_store_issuer is not self._registered_store_issuer:
            raise BatchProjectError(
                "project-stage run-plan publication requires the issuing state-store"
            )
        self._authorize(workspace_lease)
        if type(plan) is ProjectStageRunPlan:
            for target in plan.targets:
                self._authenticate_project_stage_plan_target(target)
        ref = self._publish_project_stage_runtime_record(
            plan,
            record_type=ProjectStageRunPlan,
            digest_field="plan_sha256",
            label="project-stage run plan",
        )
        payload = _canonical_bytes(plan.to_mapping())
        published = PublishedProjectStageRunPlan(
            plan=plan,
            plan_ref=ref,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            byte_count=len(payload),
            target_authorization_set_sha256=plan.target_authorization_set_sha256,
        )
        if self.reopen_project_stage_run_plan(published, workspace_lease=workspace_lease) != plan:
            raise BatchProjectError("project-stage run plan changed")
        return published

    @_guarded_catalogue_operation
    def reopen_project_stage_run_plan(
        self, published: object, *, workspace_lease: object
    ) -> object:
        from .project_stage_plan import (
            ProjectStageRunPlan,
            PublishedProjectStageRunPlan,
        )

        self._authorize(workspace_lease)
        if not isinstance(published, PublishedProjectStageRunPlan):
            raise BatchProjectError("published project-stage run plan is invalid")
        record = self._reopen_project_stage_runtime_record(
            published.plan_ref,
            record_type=ProjectStageRunPlan,
            digest_field="plan_sha256",
            expected_digest=published.plan.plan_sha256,
            label="project-stage run plan",
        )
        for target in record.targets:
            self._authenticate_project_stage_plan_target(target)
        payload = _canonical_bytes(record.to_mapping())
        if (
            record != published.plan
            or hashlib.sha256(payload).hexdigest() != published.payload_sha256
            or len(payload) != published.byte_count
            or record.target_authorization_set_sha256 != published.target_authorization_set_sha256
        ):
            raise BatchProjectError("published project-stage run plan differs")
        return record

    @_guarded_catalogue_operation
    def publish_project_stage_prepared_descriptor(
        self, descriptor: object, *, workspace_lease: object
    ) -> object:
        from .project_stage_plan import (
            PreparedProjectStageRunDescriptor,
            PublishedPreparedProjectStageRunDescriptor,
        )

        self._authorize(workspace_lease)
        ref = self._publish_project_stage_runtime_record(
            descriptor,
            record_type=PreparedProjectStageRunDescriptor,
            digest_field="prepared_descriptor_sha256",
            label="project-stage prepared descriptor",
        )
        payload = _canonical_bytes(descriptor.to_mapping())
        published = PublishedPreparedProjectStageRunDescriptor(
            descriptor=descriptor,
            descriptor_ref=ref,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            byte_count=len(payload),
        )
        if (
            self.reopen_project_stage_prepared_descriptor(
                published, workspace_lease=workspace_lease
            )
            != descriptor
        ):
            raise BatchProjectError("project-stage prepared descriptor changed")
        return published

    @_guarded_catalogue_operation
    def reopen_project_stage_prepared_descriptor(
        self, published: object, *, workspace_lease: object
    ) -> object:
        from .project_stage_plan import (
            PreparedProjectStageRunDescriptor,
            PublishedPreparedProjectStageRunDescriptor,
        )

        self._authorize(workspace_lease)
        if not isinstance(published, PublishedPreparedProjectStageRunDescriptor):
            raise BatchProjectError("published prepared descriptor is invalid")
        record = self._reopen_project_stage_runtime_record(
            published.descriptor_ref,
            record_type=PreparedProjectStageRunDescriptor,
            digest_field="prepared_descriptor_sha256",
            expected_digest=published.descriptor.prepared_descriptor_sha256,
            label="project-stage prepared descriptor",
        )
        payload = _canonical_bytes(record.to_mapping())
        if (
            record != published.descriptor
            or hashlib.sha256(payload).hexdigest() != published.payload_sha256
            or len(payload) != published.byte_count
        ):
            raise BatchProjectError("published prepared descriptor differs")
        return record

    def _project_stage_process_spec(self, record: object) -> tuple[type, str, str]:
        from mif_pipeline.qupath_runtime.project_stage import (
            ProjectStageMutationEvidenceRecord,
            ProjectStageProcessAttemptRecord,
            ProjectStageProcessDrainRecord,
        )

        for record_type, digest_field, label in (
            (ProjectStageProcessAttemptRecord, "attempt_sha256", "project-stage process attempt"),
            (ProjectStageProcessDrainRecord, "process_drain_sha256", "project-stage process drain"),
            (
                ProjectStageMutationEvidenceRecord,
                "mutation_evidence_sha256",
                "project-stage mutation evidence",
            ),
        ):
            if type(record) is record_type:
                return record_type, digest_field, label
        raise BatchProjectError("project-stage runtime evidence type is unsupported")

    @_guarded_catalogue_operation
    def publish_project_stage_process_record(
        self, record: object, *, workspace_lease: object
    ) -> AuthorityMemberRefV2:
        self._authorize(workspace_lease)
        record_type, digest_field, label = self._project_stage_process_spec(record)
        return self._publish_project_stage_runtime_record(
            record,
            record_type=record_type,
            digest_field=digest_field,
            label=label,
        )

    @_guarded_catalogue_operation
    def reopen_project_stage_process_record(
        self,
        ref: AuthorityMemberRefV2,
        *,
        expected_record: object,
        workspace_lease: object,
    ) -> object:
        self._authorize(workspace_lease)
        record_type, digest_field, label = self._project_stage_process_spec(expected_record)
        record = self._reopen_project_stage_runtime_record(
            ref,
            record_type=record_type,
            digest_field=digest_field,
            expected_digest=getattr(expected_record, digest_field),
            label=label,
        )
        if record != expected_record:
            raise BatchProjectError(f"{label} bytes differ")
        return record

    @_guarded_catalogue_operation
    def publish_project_stage_process_launch(
        self, record: object, *, workspace_lease: object
    ) -> AuthorityMemberRefV2:
        from mif_pipeline.qupath_runtime.project_stage import (
            ProjectStageProcessLaunchRecord,
        )

        self._authorize(workspace_lease)
        if type(record) is not ProjectStageProcessLaunchRecord:
            raise BatchProjectError("project-stage process launch must be typed")
        expected_ref = self._project_stage_exchange_ref(
            active_mutation_run_sha256=record.active_mutation_run_sha256,
            request_identity_sha256=record.launch_request_identity_sha256,
            direction="result",
        )
        if (
            record.process_launch_ref != expected_ref
            or record.authority_layout_sha256 != self.authority_layout_sha256
            or record.workspace_storage_scope_sha256 != self.workspace_storage_scope_sha256
        ):
            raise BatchProjectError("project-stage process launch authority differs")
        self._publish_ref(
            expected_ref,
            _canonical_bytes(record.to_mapping()),
            max_bytes=self._authority_layout.layout.family("runtime_exchange").max_member_bytes,
            label="project-stage process launch",
        )
        return expected_ref

    @_guarded_catalogue_operation
    def reopen_project_stage_process_launch(
        self,
        ref: AuthorityMemberRefV2,
        *,
        expected_record: object,
        workspace_lease: object,
    ) -> object:
        from mif_pipeline.qupath_runtime.project_stage import (
            ProjectStageProcessLaunchRecord,
        )

        self._authorize(workspace_lease)
        if type(expected_record) is not ProjectStageProcessLaunchRecord:
            raise BatchProjectError("expected project-stage launch is invalid")
        expected_ref = self._project_stage_exchange_ref(
            active_mutation_run_sha256=expected_record.active_mutation_run_sha256,
            request_identity_sha256=expected_record.launch_request_identity_sha256,
            direction="result",
        )
        if ref != expected_ref:
            raise BatchProjectError("project-stage process launch ref differs")
        payload = self._read_ref(
            ref,
            max_bytes=self._authority_layout.layout.family("runtime_exchange").max_member_bytes,
            label="project-stage process launch",
        )
        assert payload is not None
        try:
            record = ProjectStageProcessLaunchRecord.from_mapping(
                json.loads(payload.decode("utf-8", errors="strict"))
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise BatchProjectError("project-stage process launch is invalid") from exc
        if record != expected_record or _canonical_bytes(record.to_mapping()) != payload:
            raise BatchProjectError("project-stage process launch bytes differ")
        return record

    def _project_stage_log_ref(
        self,
        *,
        active_mutation_run_sha256: str,
        launch_nonce_sha256: str,
        stream: Literal["stdout", "stderr"],
    ) -> AuthorityMemberRefV2:
        try:
            return authority_member_ref_v2(
                self._authority_layout.layout,
                family="runtime_log",
                scope={
                    "workspace_storage_scope_sha256": self.workspace_storage_scope_sha256,
                    "active_mutation_run_sha256": _sha(
                        active_mutation_run_sha256, "active_mutation_run_sha256"
                    ),
                    "launch_nonce_sha256": _sha(launch_nonce_sha256, "launch_nonce_sha256"),
                },
                natural_key={"stream": stream},
            )
        except DurableRecordError as exc:
            raise BatchProjectError("project-stage log ref is invalid") from exc

    @_guarded_catalogue_operation
    def recover_contained_process_logs(self, request: object) -> tuple[str, str, bool, bool]:
        """Reopen the exact issued log members in their retained layout."""
        from mif_pipeline.qupath_runtime.catalogue_operations import _recover_contained_process_logs

        paths = []
        for stream in ("stdout", "stderr"):
            ref = self._project_stage_log_ref(
                active_mutation_run_sha256=request.authority.active_mutation_run_sha256,
                launch_nonce_sha256=request.authority.launch_nonce_sha256,
                stream=stream,
            )
            if ref.physical_name != getattr(request, f"{stream}_log_name"):
                raise BatchProjectError("recovery log differs from its issued member")
            paths.append(self._bucket_for_ref(ref).member_path(ref))
        if paths[0].parent != paths[1].parent:
            raise BatchProjectError("recovery logs belong to different authority buckets")
        return _recover_contained_process_logs(paths[0].parent, request)

    @_guarded_catalogue_operation
    def reopen_designation_request(
        self,
        mutation_request_sha256: str,
        *,
        workspace_lease: object,
    ) -> tuple[ProjectDesignationRequest, ProjectMutationRequestCapability]:
        """Return typed designation and capability from one bounded authority."""

        self._authorize(workspace_lease)
        request_sha256 = _sha(mutation_request_sha256, "mutation_request_sha256")
        payload = self._reopen_request_bytes(request_sha256)
        try:
            request = ProjectDesignationRequest.from_mapping(
                json.loads(payload.decode("utf-8", errors="strict"))
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise BatchProjectError("persisted designation request is invalid") from exc
        if request.request_sha256 != request_sha256:
            raise BatchProjectError("persisted designation request identity differs")
        capability = self.reopen_request(
            request_sha256,
            mutation_authorization_sha256=request.authorizing_record_sha256,
            workspace_lease=workspace_lease,
        )
        return request, capability

    @_guarded_catalogue_operation
    def publish_runtime_evidence(
        self,
        evidence: object,
        *,
        workspace_lease: object,
    ) -> AuthorityMemberRefV2:
        self._authorize(workspace_lease)
        from mif_pipeline.qupath_runtime.catalogue import QuPathCatalogueEvidenceEnvelope

        if not isinstance(evidence, QuPathCatalogueEvidenceEnvelope):
            raise BatchProjectError("runtime evidence must be typed")
        ref = self.runtime_evidence_ref(evidence.evidence_sha256)
        self._publish_ref(
            ref,
            _canonical_bytes(evidence.to_mapping()),
            max_bytes=_RUNTIME_EVIDENCE_MAX_BYTES,
            label="runtime catalogue evidence",
        )
        self._known_runtime_evidence.setdefault(evidence.request_envelope_sha256, {})[
            evidence.evidence_sha256
        ] = evidence
        return ref

    @_guarded_catalogue_operation
    def publish_runtime_request(
        self,
        request: QuPathCatalogueRequestEnvelope,
        *,
        workspace_lease: object,
    ) -> AuthorityMemberRefV2:
        """Retain the exact runtime request needed for crash-safe replay."""
        self._authorize(workspace_lease)
        if not isinstance(request, QuPathCatalogueRequestEnvelope):
            raise BatchProjectError("runtime request must be typed")
        ref = self.request_ref(request.request_envelope_sha256)
        self._publish_ref(
            ref,
            _canonical_bytes(request.to_mapping()),
            max_bytes=_RUNTIME_REQUEST_MAX_BYTES,
            label="runtime catalogue request",
        )
        self._known_runtime_requests.setdefault(request.mutation_request_sha256, {})[
            request.request_envelope_sha256
        ] = request
        return ref

    def _operation_replay_root(self, mutation_request_sha256: str) -> Path:
        digest = _sha(mutation_request_sha256, "mutation_request_sha256")
        return self._root / "project-operation-replays" / digest

    def _read_canonical_bytes(
        self,
        path: AuthorityMemberRefV2,
        field: str,
        *,
        max_bytes: int,
    ) -> tuple[bytes, Mapping[str, object]]:
        if isinstance(path, AuthorityMemberRefV2):
            payload = self._read_ref(path, max_bytes=max_bytes, label=field)
            assert payload is not None
        else:
            raise BatchProjectError(f"{field} must use retained authority reference")
        try:
            mapping = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BatchProjectError(f"{field} is invalid JSON") from exc
        if not isinstance(mapping, Mapping) or _canonical_bytes(mapping) != payload:
            raise BatchProjectError(f"{field} is not canonical")
        return payload, mapping

    def _read_canonical_mapping(
        self,
        path: AuthorityMemberRefV2,
        field: str,
        *,
        max_bytes: int,
    ) -> Mapping[str, object]:
        return self._read_canonical_bytes(path, field, max_bytes=max_bytes)[1]

    @_guarded_catalogue_operation
    def publish_operation_replay_plan(
        self,
        plan: BatchProjectOperationReplayPlan,
        *,
        workspace_lease: object,
    ) -> AuthorityMemberRefV2:
        self._authorize(workspace_lease)
        if not isinstance(plan, BatchProjectOperationReplayPlan):
            raise BatchProjectError("operation replay plan must be typed")
        path: AuthorityMemberRefV2 = self.replay_plan_ref(plan.mutation_request_sha256)
        self._publish_ref(
            path,
            _canonical_bytes(plan.to_mapping()),
            max_bytes=_OPERATION_REPLAY_PLAN_MAX_BYTES,
            label="operation replay plan",
        )
        reopened = self.reopen_operation_replay_plan(
            plan.mutation_request_sha256,
            workspace_lease=workspace_lease,
        )
        if reopened != plan:
            raise BatchProjectError("operation replay plan changed after publication")
        return path

    @_guarded_catalogue_operation
    def reopen_operation_replay_plan(
        self,
        mutation_request_sha256: str,
        *,
        workspace_lease: object,
    ) -> BatchProjectOperationReplayPlan:
        self._authorize(workspace_lease)
        digest = _sha(mutation_request_sha256, "mutation_request_sha256")
        path: AuthorityMemberRefV2 = (
            self.replay_plan_ref(digest)
        )
        plan = BatchProjectOperationReplayPlan.from_mapping(
            self._read_canonical_mapping(
                path,
                "operation replay plan",
                max_bytes=_OPERATION_REPLAY_PLAN_MAX_BYTES,
            )
        )
        if plan.mutation_request_sha256 != digest:
            raise BatchProjectError("operation replay plan request differs")
        return plan

    @_guarded_catalogue_operation
    def reopen_operation_checkpoints(
        self,
        plan: BatchProjectOperationReplayPlan,
        *,
        workspace_lease: object,
    ) -> tuple[
        tuple[
            BatchProjectOperationCheckpoint,
            QuPathCatalogueRequestEnvelope,
            QuPathCatalogueEvidenceEnvelope,
        ],
        ...,
    ]:
        self._authorize(workspace_lease)
        if (
            self.reopen_operation_replay_plan(
                plan.mutation_request_sha256,
                workspace_lease=workspace_lease,
            )
            != plan
        ):
            raise BatchProjectError("operation replay plan authority differs")
        paths: tuple[AuthorityMemberRefV2, ...] = ()
        found: list[AuthorityMemberRefV2] = []
        missing = False
        for ordinal in range(len(plan.steps)):
            ref = self.checkpoint_ref(plan.mutation_request_sha256, ordinal)
            payload = self._read_ref(
                ref,
                max_bytes=_OPERATION_CHECKPOINT_MAX_BYTES,
                label="operation checkpoint",
                optional=True,
            )
            if payload is None:
                missing = True
                continue
            if missing:
                raise BatchProjectError(
                    "operation checkpoints are missing, extra, or out of order"
                )
            found.append(ref)
        paths = tuple(found)
        reopened: list[
            tuple[
                BatchProjectOperationCheckpoint,
                QuPathCatalogueRequestEnvelope,
                QuPathCatalogueEvidenceEnvelope,
            ]
        ] = []
        predecessor: str | None = None
        for ordinal, path in enumerate(paths):
            step = plan.steps[ordinal]
            checkpoint = BatchProjectOperationCheckpoint.from_mapping(
                self._read_canonical_mapping(
                    path,
                    "operation checkpoint",
                    max_bytes=_OPERATION_CHECKPOINT_MAX_BYTES,
                )
            )
            if (
                checkpoint.replay_plan_sha256 != plan.replay_plan_sha256
                or checkpoint.mutation_request_sha256 != plan.mutation_request_sha256
                or checkpoint.ordinal != ordinal
                or checkpoint.role != step.role
                or checkpoint.operation != step.operation
                or checkpoint.request_identity_sha256 != step.request_identity_sha256
                or checkpoint.predecessor_checkpoint_sha256 != predecessor
            ):
                raise BatchProjectError("operation checkpoint authority differs")
            request_path: AuthorityMemberRefV2 = (
                self.request_ref(checkpoint.request_envelope_sha256)
            )
            evidence_path: AuthorityMemberRefV2 = (
                self.runtime_evidence_ref(checkpoint.evidence_sha256)
            )
            request_mapping = self._read_canonical_mapping(
                request_path,
                "operation request",
                max_bytes=_RUNTIME_REQUEST_MAX_BYTES,
            )
            evidence_mapping = self._read_canonical_mapping(
                evidence_path,
                "operation evidence",
                max_bytes=_RUNTIME_EVIDENCE_MAX_BYTES,
            )
            request_bytes = _canonical_bytes(request_mapping)
            evidence_bytes = _canonical_bytes(evidence_mapping)
            try:
                request = QuPathCatalogueRequestEnvelope.from_mapping(request_mapping)
                evidence = QuPathCatalogueEvidenceEnvelope.from_mapping(evidence_mapping)
            except ProjectAuthorityError as exc:
                raise BatchProjectError("operation checkpoint transport is invalid") from exc
            if (
                len(request_bytes) != checkpoint.request_byte_count
                or hashlib.sha256(request_bytes).hexdigest() != checkpoint.request_payload_sha256
                or len(evidence_bytes) != checkpoint.evidence_byte_count
                or hashlib.sha256(evidence_bytes).hexdigest() != checkpoint.evidence_payload_sha256
                or request.request_envelope_sha256 != checkpoint.request_envelope_sha256
                or evidence.evidence_sha256 != checkpoint.evidence_sha256
                or evidence.request_envelope_sha256 != request.request_envelope_sha256
                or request.operation != step.operation
                or evidence.operation != step.operation
                or _runtime_request_identity(request, step.role) != step.request_identity_sha256
            ):
                raise BatchProjectError("operation checkpoint bytes differ")
            reopened.append((checkpoint, request, evidence))
            predecessor = checkpoint.checkpoint_sha256
        return tuple(reopened)

    @_guarded_catalogue_operation
    def _reopen_provider_runtime_authority(
        self,
        request_capability: ProjectMutationRequestCapability,
    ) -> tuple[
        bytes,
        bytes,
        tuple[tuple[bytes, bytes, bytes], ...],
        bytes,
    ]:
        """Return pinned canonical replay bytes to the owning provider.

        This is the narrow inverted-dependency bridge used by
        ``qupath_runtime``.  The provider receives bytes, not caller-authored
        typed projections, and independently validates their authority.
        """

        self._authenticate_request_capability(request_capability)
        mutation_request_sha256 = request_capability.mutation_request_sha256
        mutation_request_bytes = self._reopen_request_bytes(mutation_request_sha256)
        plan = self.reopen_operation_replay_plan(
            mutation_request_sha256,
            workspace_lease=self._capability,
        )
        checkpoints = self.reopen_operation_checkpoints(
            plan,
            workspace_lease=self._capability,
        )
        if len(checkpoints) != len(plan.steps):
            raise BatchProjectError(
                "provider runtime authority requires the complete checkpoint chain"
            )
        replay_catalogue, replay_pairs = self.reopen_runtime_replay(
            mutation_request_sha256,
            workspace_lease=self._capability,
        )
        checkpoint_upserts = tuple(
            (request, evidence)
            for _checkpoint, request, evidence in checkpoints
            if request.operation == "upsert_entry"
        )
        if (
            checkpoint_upserts != replay_pairs
            or replay_catalogue.mutation_request_sha256 != mutation_request_sha256
            or replay_catalogue.mutation_authorization_sha256
            != request_capability.mutation_authorization_sha256
        ):
            raise BatchProjectError("provider runtime replay differs from its checkpoint chain")
        replay_root = self._operation_replay_root(mutation_request_sha256)
        plan_bytes, _ = self._read_canonical_bytes(
            (
                self.replay_plan_ref(mutation_request_sha256)
            ),
            "provider operation replay plan",
            max_bytes=_OPERATION_REPLAY_PLAN_MAX_BYTES,
        )
        checkpoint_bytes: list[tuple[bytes, bytes, bytes]] = []
        for checkpoint, request, evidence in checkpoints:
            persisted_checkpoint, _ = self._read_canonical_bytes(
                (
                    self.checkpoint_ref(mutation_request_sha256, checkpoint.ordinal)
                ),
                "provider operation checkpoint",
                max_bytes=_OPERATION_CHECKPOINT_MAX_BYTES,
            )
            persisted_request, _ = self._read_canonical_bytes(
                (
                    self.request_ref(request.request_envelope_sha256)
                ),
                "provider runtime request",
                max_bytes=_RUNTIME_REQUEST_MAX_BYTES,
            )
            persisted_evidence, _ = self._read_canonical_bytes(
                (
                    self.runtime_evidence_ref(evidence.evidence_sha256)
                ),
                "provider runtime evidence",
                max_bytes=_RUNTIME_EVIDENCE_MAX_BYTES,
            )
            checkpoint_bytes.append(
                (
                    persisted_checkpoint,
                    persisted_request,
                    persisted_evidence,
                )
            )
        replay_bytes, _ = self._read_canonical_bytes(
            (
                self.runtime_replay_ref(mutation_request_sha256)
            ),
            "provider runtime replay",
            max_bytes=_RUNTIME_REPLAY_MAX_BYTES,
        )
        return (
            mutation_request_bytes,
            plan_bytes,
            tuple(checkpoint_bytes),
            replay_bytes,
        )

    @_guarded_catalogue_operation
    def _reopen_provider_unlaunched_authority(
        self,
        request_capability: ProjectMutationRequestCapability,
        runtime_request: object,
    ) -> bytes:
        """Reprove one pending request against the complete local transport set."""

        self._authenticate_request_capability(request_capability)
        if self._issued_request_kinds.get(id(request_capability)) == "annotation_classifier":
            self._authenticate_request_capability(
                request_capability, expected_kind="annotation_classifier"
            )
            if type(runtime_request) is not QuPathNativeTrainingRequest or runtime_request.authority_layout_sha256 != self.authority_layout_sha256 or runtime_request.workspace_storage_scope_sha256 != self.workspace_storage_scope_sha256 or (runtime_request.mutation_request_sha256 != request_capability.mutation_request_sha256) or (runtime_request.mutation_authorization_sha256 != request_capability.mutation_authorization_sha256) or (runtime_request.launch_nonce_sha256 is None):
                raise BatchProjectError("provider unlaunched request authority differs")
            expected_ref = authority_member_ref_v2(
                self._authority_layout.layout,
                family="runtime_exchange",
                scope={
                    "workspace_storage_scope_sha256": (self.workspace_storage_scope_sha256),
                    "active_mutation_run_sha256": (runtime_request.active_mutation_run_sha256),
                    "role": "classifier_training",
                    "request_identity_sha256": (runtime_request.request_identity_sha256),
                },
                natural_key={"direction": "request"},
            )
            if runtime_request.request_member_ref != expected_ref:
                raise BatchProjectError("provider unlaunched runtime request ref differs")
            retained = self._read_ref(
                expected_ref,
                max_bytes=self._authority_layout.layout.family("runtime_exchange").max_member_bytes,
                label="provider unlaunched native-training request",
                optional=True,
            )
            canonical = runtime_request.to_bytes()
            if (
                retained is None
                or retained != canonical
                or decode_qupath_native_training_request(retained) != runtime_request
            ):
                raise BatchProjectError("provider unlaunched runtime request bytes differ")
            return retained
        if (
            not isinstance(runtime_request, QuPathCatalogueRequestEnvelope)
            or runtime_request.mutation_request_sha256 != request_capability.mutation_request_sha256
            or runtime_request.mutation_authorization_sha256
            != request_capability.mutation_authorization_sha256
            or runtime_request.launch_nonce_sha256 is None
        ):
            raise BatchProjectError("provider unlaunched request authority differs")
        runtime_request_path: AuthorityMemberRefV2 = (
            self.request_ref(runtime_request.request_envelope_sha256)
        )
        retained = self._read_ref(
            runtime_request_path,
            max_bytes=_RUNTIME_REQUEST_MAX_BYTES,
            label="provider unlaunched runtime request",
            optional=True,
        )
        if retained is None:
            return _canonical_bytes(runtime_request.to_mapping())
        plan = self.reopen_operation_replay_plan(
            request_capability.mutation_request_sha256,
            workspace_lease=self._capability,
        )
        matching_steps = tuple(
            step
            for step in plan.steps
            if runtime_request.operation == step.operation
            and _runtime_request_identity(runtime_request, step.role)
            == step.request_identity_sha256
        )
        if len(matching_steps) != 1:
            raise BatchProjectError("provider unlaunched request replay step is ambiguous")
        checkpoints = self.reopen_operation_checkpoints(plan, workspace_lease=self._capability)
        if len(checkpoints) != matching_steps[0].ordinal:
            raise BatchProjectError("provider unlaunched request has checkpoint evidence")
        evidence = self._known_runtime_evidence.get(runtime_request.request_envelope_sha256, {})
        if evidence:
            raise BatchProjectError("provider unlaunched request has durable evidence")
        payload, mapping = self._read_canonical_bytes(
            runtime_request_path,
            "provider unlaunched runtime request",
            max_bytes=_RUNTIME_REQUEST_MAX_BYTES,
        )
        if QuPathCatalogueRequestEnvelope.from_mapping(mapping) != runtime_request:
            raise BatchProjectError("provider unlaunched request bytes differ")
        return payload

    @_guarded_catalogue_operation
    def publish_operation_checkpoint(
        self,
        plan: BatchProjectOperationReplayPlan,
        step: BatchProjectOperationReplayStep,
        request: QuPathCatalogueRequestEnvelope,
        evidence: QuPathCatalogueEvidenceEnvelope,
        *,
        workspace_lease: object,
    ) -> BatchProjectOperationCheckpoint:
        self._authorize(workspace_lease)
        if (
            not isinstance(plan, BatchProjectOperationReplayPlan)
            or step not in plan.steps
            or plan.steps[step.ordinal] != step
            or not isinstance(request, QuPathCatalogueRequestEnvelope)
            or not isinstance(evidence, QuPathCatalogueEvidenceEnvelope)
            or request.operation != step.operation
            or evidence.operation != step.operation
            or evidence.request_envelope_sha256 != request.request_envelope_sha256
            or _runtime_request_identity(request, step.role) != step.request_identity_sha256
        ):
            raise BatchProjectError("operation checkpoint inputs differ")
        completed = self.reopen_operation_checkpoints(plan, workspace_lease=workspace_lease)
        if len(completed) != step.ordinal:
            if len(completed) > step.ordinal and completed[step.ordinal][1:] == (request, evidence):
                return completed[step.ordinal][0]
            raise BatchProjectError("operation checkpoint is not the next step")
        request_path = self.publish_runtime_request(request, workspace_lease=workspace_lease)
        evidence_path = self.publish_runtime_evidence(evidence, workspace_lease=workspace_lease)
        request_bytes, _ = self._read_canonical_bytes(
            request_path,
            "operation request",
            max_bytes=_RUNTIME_REQUEST_MAX_BYTES,
        )
        evidence_bytes, _ = self._read_canonical_bytes(
            evidence_path,
            "operation evidence",
            max_bytes=_RUNTIME_EVIDENCE_MAX_BYTES,
        )
        checkpoint = BatchProjectOperationCheckpoint.create(
            replay_plan_sha256=plan.replay_plan_sha256,
            mutation_request_sha256=plan.mutation_request_sha256,
            ordinal=step.ordinal,
            role=step.role,
            operation=step.operation,
            request_identity_sha256=step.request_identity_sha256,
            request_envelope_sha256=request.request_envelope_sha256,
            request_payload_sha256=hashlib.sha256(request_bytes).hexdigest(),
            request_byte_count=len(request_bytes),
            evidence_sha256=evidence.evidence_sha256,
            evidence_payload_sha256=hashlib.sha256(evidence_bytes).hexdigest(),
            evidence_byte_count=len(evidence_bytes),
            predecessor_checkpoint_sha256=(
                None if not completed else completed[-1][0].checkpoint_sha256
            ),
        )
        ref = self.checkpoint_ref(plan.mutation_request_sha256, step.ordinal)
        self._publish_ref(
            ref,
            _canonical_bytes(checkpoint.to_mapping()),
            max_bytes=_OPERATION_CHECKPOINT_MAX_BYTES,
            label="operation checkpoint",
        )
        return checkpoint

    @_guarded_catalogue_operation
    def recover_pending_operation_checkpoint(
        self,
        plan: BatchProjectOperationReplayPlan,
        step: BatchProjectOperationReplayStep,
        *,
        workspace_lease: object,
    ) -> (
        tuple[
            QuPathCatalogueRequestEnvelope,
            QuPathCatalogueEvidenceEnvelope,
        ]
        | None
    ):
        """Checkpoint one complete child pair left before its journal write."""

        _requests, complete = self._pending_operation_transport(
            plan, step, workspace_lease=workspace_lease
        )
        if complete is None:
            return None
        request, evidence = complete
        self.publish_operation_checkpoint(
            plan,
            step,
            request,
            evidence,
            workspace_lease=workspace_lease,
        )
        self._retire_checkpointed_pending_evidence(
            request, evidence, workspace_lease=workspace_lease
        )
        return request, evidence

    def _retire_checkpointed_pending_evidence(
        self,
        request: QuPathCatalogueRequestEnvelope,
        evidence: QuPathCatalogueEvidenceEnvelope,
        *,
        workspace_lease: object,
    ) -> None:
        """Remove exact child temporary evidence after its checkpoint is durable."""

        self._authorize(workspace_lease)
        if (
            not isinstance(request, QuPathCatalogueRequestEnvelope)
            or not isinstance(evidence, QuPathCatalogueEvidenceEnvelope)
            or evidence.request_envelope_sha256 != request.request_envelope_sha256
        ):
            raise BatchProjectError("checkpointed pending evidence authority differs")
        return

    def _reopen_unlaunched_operation_request(
        self,
        plan: BatchProjectOperationReplayPlan,
        step: BatchProjectOperationReplayStep,
        *,
        workspace_lease: object,
    ) -> QuPathCatalogueRequestEnvelope | None:
        """Return the sole request lacking both evidence and a checkpoint."""

        requests, complete = self._pending_operation_transport(
            plan, step, workspace_lease=workspace_lease
        )
        if complete is not None:
            return None
        if len(requests) > 1:
            raise BatchProjectError("pending operation request is ambiguous")
        return None if not requests else requests[0]

    def _retire_unlaunched_operation_request(
        self,
        plan: BatchProjectOperationReplayPlan,
        step: BatchProjectOperationReplayStep,
        request: QuPathCatalogueRequestEnvelope,
        *,
        workspace_lease: object,
    ) -> None:
        """Durably remove one provider-proven never-launched request orphan."""

        self._authorize(workspace_lease)
        observed = self._reopen_unlaunched_operation_request(
            plan, step, workspace_lease=workspace_lease
        )
        if observed != request:
            raise BatchProjectError("unlaunched operation request changed")
        request_ref = self.request_ref(request.request_envelope_sha256)
        payload = self._read_ref(
            request_ref,
            max_bytes=_RUNTIME_REQUEST_MAX_BYTES,
            label="unlaunched operation request",
        )
        assert payload is not None
        self._publish_retirement(
            request_ref,
            retired_payload_sha256=hashlib.sha256(payload).hexdigest(),
            closure_kind="abort",
            closure_authority_sha256=plan.replay_plan_sha256,
        )
        return

    def _pending_operation_transport(
        self,
        plan: BatchProjectOperationReplayPlan,
        step: BatchProjectOperationReplayStep,
        *,
        workspace_lease: object,
    ) -> tuple[
        tuple[QuPathCatalogueRequestEnvelope, ...],
        tuple[
            QuPathCatalogueRequestEnvelope,
            QuPathCatalogueEvidenceEnvelope,
        ]
        | None,
    ]:
        """Reopen the exact current-operation request/evidence inventory."""

        self._authorize(workspace_lease)
        if (
            not isinstance(plan, BatchProjectOperationReplayPlan)
            or not isinstance(step, BatchProjectOperationReplayStep)
            or step not in plan.steps
            or plan.steps[step.ordinal] != step
        ):
            raise BatchProjectError("pending operation authority is invalid")
        completed = self.reopen_operation_checkpoints(plan, workspace_lease=workspace_lease)
        checkpointed = {item[1].request_envelope_sha256 for item in completed}
        candidates = tuple(
            request
            for request in self._known_runtime_requests.get(
                plan.mutation_request_sha256, {}
            ).values()
            if request.request_envelope_sha256 not in checkpointed
            and (
                "batch_project_request",
                self.request_ref(request.request_envelope_sha256).key_sha256,
            )
            not in self._retired_member_keys
            and request.operation == step.operation
            and _runtime_request_identity(request, step.role) == step.request_identity_sha256
        )
        if len(candidates) > 1:
            raise BatchProjectError("pending operation request is ambiguous")
        if not candidates:
            return (), None
        request = candidates[0]
        evidence_values = tuple(
            self._known_runtime_evidence.get(request.request_envelope_sha256, {}).values()
        )
        if len(evidence_values) > 1:
            raise BatchProjectError("pending operation evidence is ambiguous")
        if not evidence_values:
            return candidates, None
        evidence = evidence_values[0]
        _validate_operation_request_evidence_pair(request, evidence, step.role)
        return candidates, (request, evidence)

    @_guarded_catalogue_operation
    def publish_runtime_replay(
        self,
        catalogue: BatchProjectCatalogue,
        runtime_evidence: tuple[
            tuple[QuPathCatalogueRequestEnvelope, QuPathCatalogueEvidenceEnvelope],
            ...,
        ],
        *,
        workspace_lease: object,
    ) -> AuthorityMemberRefV2:
        """Publish the exact request/evidence set before global authority advances."""
        self._authorize(workspace_lease)
        if not runtime_evidence:
            raise BatchProjectError("runtime replay requires exact evidence")
        pairs: list[BatchProjectRuntimeReplayPair] = []
        for request, evidence in runtime_evidence:
            if (
                request.operation != "upsert_entry"
                or evidence.operation != "upsert_entry"
                or evidence.request_envelope_sha256 != request.request_envelope_sha256
                or request.mutation_request_sha256 != catalogue.mutation_request_sha256
                or request.mutation_authorization_sha256 != catalogue.mutation_authorization_sha256
                or request.project_identity_sha256 != catalogue.project_identity_sha256
            ):
                raise BatchProjectError("runtime replay evidence is invalid")
            request_path = self.publish_runtime_request(request, workspace_lease=workspace_lease)
            evidence_path = self.publish_runtime_evidence(evidence, workspace_lease=workspace_lease)
            request_bytes, _ = self._read_canonical_bytes(
                request_path,
                "runtime replay request",
                max_bytes=_RUNTIME_REQUEST_MAX_BYTES,
            )
            evidence_bytes, _ = self._read_canonical_bytes(
                evidence_path,
                "runtime replay evidence",
                max_bytes=_RUNTIME_EVIDENCE_MAX_BYTES,
            )
            pairs.append(
                BatchProjectRuntimeReplayPair(
                    request_envelope_sha256=request.request_envelope_sha256,
                    request_payload_sha256=hashlib.sha256(request_bytes).hexdigest(),
                    request_byte_count=len(request_bytes),
                    evidence_sha256=evidence.evidence_sha256,
                    evidence_payload_sha256=hashlib.sha256(evidence_bytes).hexdigest(),
                    evidence_byte_count=len(evidence_bytes),
                )
            )
        replay = BatchProjectRuntimeReplay.create(
            catalogue=catalogue,
            runtime_pairs=tuple(pairs),
        )
        ref = self.runtime_replay_ref(catalogue.mutation_request_sha256)
        self._publish_ref(
            ref,
            _canonical_bytes(replay.to_mapping()),
            max_bytes=_RUNTIME_REPLAY_MAX_BYTES,
            label="runtime replay record",
        )
        return ref

    @_guarded_catalogue_operation
    def reopen_runtime_replay(
        self,
        mutation_request_sha256: str,
        *,
        workspace_lease: object,
    ) -> tuple[
        BatchProjectCatalogue,
        tuple[
            tuple[QuPathCatalogueRequestEnvelope, QuPathCatalogueEvidenceEnvelope],
            ...,
        ],
    ]:
        """Authenticate and reopen one exact pre-authority runtime result."""
        self._authorize(workspace_lease)
        digest = _sha(mutation_request_sha256, "mutation_request_sha256")
        path: AuthorityMemberRefV2 = (
            self.runtime_replay_ref(digest)
        )
        payload, mapping = self._read_canonical_bytes(
            path,
            "runtime replay record",
            max_bytes=_RUNTIME_REPLAY_MAX_BYTES,
        )
        replay = BatchProjectRuntimeReplay.from_mapping(mapping)
        if replay.mutation_request_sha256 != digest:
            raise BatchProjectError("runtime replay request identity differs")
        pairs = []
        expected_request_paths: set[Path] = set()
        expected_evidence_paths: set[Path] = set()
        for pair in replay.runtime_pairs:
            request_digest = pair.request_envelope_sha256
            evidence_digest = pair.evidence_sha256
            request_path: AuthorityMemberRefV2 = (
                self.request_ref(request_digest)
            )
            evidence_path: AuthorityMemberRefV2 = (
                self.runtime_evidence_ref(evidence_digest)
            )
            if isinstance(request_path, Path):
                expected_request_paths.add(request_path)
            if isinstance(evidence_path, Path):
                expected_evidence_paths.add(evidence_path)
            request_bytes, request_mapping = self._read_canonical_bytes(
                request_path,
                "runtime replay request",
                max_bytes=_RUNTIME_REQUEST_MAX_BYTES,
            )
            evidence_bytes, evidence_mapping = self._read_canonical_bytes(
                evidence_path,
                "runtime replay evidence",
                max_bytes=_RUNTIME_EVIDENCE_MAX_BYTES,
            )
            if (
                len(request_bytes) != pair.request_byte_count
                or hashlib.sha256(request_bytes).hexdigest() != pair.request_payload_sha256
                or len(evidence_bytes) != pair.evidence_byte_count
                or hashlib.sha256(evidence_bytes).hexdigest() != pair.evidence_payload_sha256
            ):
                raise BatchProjectError("runtime replay evidence bytes differ")
            try:
                request = QuPathCatalogueRequestEnvelope.from_mapping(request_mapping)
                evidence = QuPathCatalogueEvidenceEnvelope.from_mapping(evidence_mapping)
            except ValueError as exc:
                raise BatchProjectError("runtime replay evidence is invalid") from exc
            if (
                request.request_envelope_sha256 != request_digest
                or evidence.evidence_sha256 != evidence_digest
                or evidence.request_envelope_sha256 != request_digest
                or request.operation != "upsert_entry"
                or evidence.operation != "upsert_entry"
                or request.mutation_request_sha256 != replay.mutation_request_sha256
                or request.mutation_authorization_sha256 != replay.mutation_authorization_sha256
                or request.project_identity_sha256 != replay.catalogue.project_identity_sha256
                or evidence.launch_nonce_sha256 != request.launch_nonce_sha256
            ):
                raise BatchProjectError("runtime replay evidence identity differs")
            pairs.append((request, evidence))
        return replay.catalogue, tuple(pairs)

    @_guarded_catalogue_operation
    def prepare_successor(
        self,
        catalogue: BatchProjectCatalogue,
        *,
        expected_catalogue_sha256: str | None,
        workspace_lease: object,
        authority_attestation: ProjectMutationAttestation | None = None,
    ) -> BatchProjectCatalogue:
        self._authorize(workspace_lease)
        current = self.load_current()
        observed = None if current is None else current.catalogue_sha256
        ordinary = (
            observed == expected_catalogue_sha256
            and catalogue.predecessor_catalogue_sha256 == expected_catalogue_sha256
        )
        replay = (
            current == catalogue
            and catalogue.predecessor_catalogue_sha256 == expected_catalogue_sha256
            and isinstance(authority_attestation, ProjectMutationAttestation)
            and authority_attestation.workspace_catalogue_sha256 == catalogue.catalogue_sha256
            and authority_attestation.project_identity_sha256 == catalogue.project_identity_sha256
            and authority_attestation.mutation_request_sha256 == catalogue.mutation_request_sha256
            and authority_attestation.mutation_authorization_sha256
            == catalogue.mutation_authorization_sha256
            and authority_attestation.post_save_qpproj_snapshot_sha256
            == catalogue.qpproj_snapshot_sha256
        )
        if not ordinary and not replay:
            raise BatchProjectCatalogueConflictError(
                "workspace-local catalogue predecessor changed"
            )
        return catalogue

    @_guarded_catalogue_operation
    def commit_successor(
        self,
        catalogue: BatchProjectCatalogue,
        *,
        expected_catalogue_sha256: str | None,
        workspace_lease: object,
        authority_attestation: ProjectMutationAttestation | None = None,
    ) -> BatchProjectCatalogue:
        self.prepare_successor(
            catalogue,
            expected_catalogue_sha256=expected_catalogue_sha256,
            workspace_lease=workspace_lease,
            authority_attestation=authority_attestation,
        )
        payload = _canonical_bytes(catalogue.to_mapping())
        generation: AuthorityMemberRefV2 = self.generation_ref(
            catalogue.catalogue_sha256
        )
        self._publish_ref(
            generation,
            payload,
            max_bytes=_CATALOGUE_RECORD_MAX_BYTES,
            label="catalogue immutable generation",
        )
        current = self.load_current()
        observed = None if current is None else current.catalogue_sha256
        if current == catalogue:
            if authority_attestation is None:
                raise BatchProjectCatalogueConflictError(
                    "catalogue replay requires global attestation"
                )
            return catalogue
        if observed != expected_catalogue_sha256:
            raise BatchProjectCatalogueConflictError(
                "workspace-local catalogue predecessor changed"
            )
        try:
            current_ref = self.current_ref
            assert current_ref is not None
            current_bucket = self._bucket_for_ref(current_ref)
            generation_bucket = self._bucket_for_ref(generation)
            current_bucket.replace_pointer_cas(
                current_ref.physical_name,
                generation_bucket,
                generation.physical_name,
                expected_predecessor_bytes=(
                    None if current is None else _canonical_bytes(current.to_mapping())
                ),
                max_bytes=_CATALOGUE_RECORD_MAX_BYTES,
                label="catalogue current pointer",
            )
            if self.load_current() != catalogue:
                raise BatchProjectError("catalogue current changed after replacement")
        except (DurableRecordError, BatchProjectError) as exc:
            raise BatchProjectCatalogueConflictError(
                "workspace-local catalogue predecessor changed"
            ) from exc
        return catalogue

    @_guarded_catalogue_operation
    def stage_successor_generation(
        self,
        catalogue: BatchProjectCatalogue,
        *,
        expected_catalogue_sha256: str | None,
        workspace_lease: object,
    ) -> AuthorityMemberRefV2:
        """Durably stage immutable candidate bytes without advancing current."""

        self._authorize(workspace_lease)
        if not isinstance(catalogue, BatchProjectCatalogue):
            raise BatchProjectError("catalogue candidate must be typed")
        current = self.load_current()
        observed = None if current is None else current.catalogue_sha256
        if catalogue.predecessor_catalogue_sha256 != expected_catalogue_sha256 or (
            current != catalogue and observed != expected_catalogue_sha256
        ):
            raise BatchProjectCatalogueConflictError(
                "workspace-local catalogue predecessor changed"
            )
        payload = _canonical_bytes(catalogue.to_mapping())
        generation: AuthorityMemberRefV2 = self.generation_ref(
            catalogue.catalogue_sha256
        )
        self._publish_ref(
            generation,
            payload,
            max_bytes=_CATALOGUE_RECORD_MAX_BYTES,
            label="catalogue immutable generation",
        )
        generation_bytes = self._read_ref(
            generation,
            max_bytes=_CATALOGUE_RECORD_MAX_BYTES,
            label="catalogue immutable generation",
        )
        assert generation_bytes is not None
        if generation_bytes != payload:
            raise BatchProjectError("staged catalogue generation bytes changed")
        return generation


_STORE_MARKER = object()


def deterministic_project_entry_name(sample_id: str, slide_id: str, role: str) -> str:
    """Return a stable human-readable name without deriving entry identity."""
    _text(sample_id, "sample_id")
    _text(slide_id, "slide_id")
    if role not in {"primary_mif", "moving_reference"}:
        raise BatchProjectError("image role is invalid")
    return f"{sample_id}__{slide_id}__{role}"


def _entry_upsert_mode(request, role, prior_by_role):
    """Replace only the selected role when its admitted image location changes."""
    prior = prior_by_role.get(role)
    source_uri = (request.primary_source_uri if role == "primary_mif"
                  else request.moving_reference.canonical_uri)
    return "replace" if prior is not None and prior.canonical_uri != source_uri else "reuse"


def source_project_history_path(project_path: Path, mutation_request_sha256: str) -> Path:
    """Keep the old project beside its original relative image-data paths."""
    return project_path.parent / f"source-history-{_sha(mutation_request_sha256, 'mutation request')}.qpproj"


def _upsert_payload(
    request: EnsureBatchEntriesRequest,
    *,
    source_uri: str,
    source_sha256: str,
    role: Literal["primary_mif", "moving_reference"],
    entry_mode: Literal["reuse", "replace"],
    expected_project_entry_id: str | None,
) -> QuPathEntryUpsertRequest:
    metadata = {
        "mif_pipeline.batch_id": request.batch_id,
        "mif_pipeline.sample_id": request.sample_id,
        "mif_pipeline.slide_id": request.slide_id,
        "mif_pipeline.validation_domain_id": request.validation_domain_id,
        "mif_pipeline.image_role": role,
        "mif_pipeline.source_image_sha256": source_sha256,
    }
    if role == "moving_reference":
        if not isinstance(request.moving_reference, PairedMovingReferenceAuthority):
            raise BatchProjectError("moving-reference upsert lacks reviewed authority")
        metadata["mif_pipeline.reference_modality"] = request.moving_reference.modality
    return QuPathEntryUpsertRequest(
        entry_mode=entry_mode,
        source_uri=source_uri,
        source_image_sha256=source_sha256,
        entry_name=deterministic_project_entry_name(request.sample_id, request.slide_id, role),
        expected_project_entry_id=expected_project_entry_id,
        entry_metadata=metadata,
        require_saved_state_attestation=(role == "primary_mif"),
    )


def _runtime_request_identity(
    request: QuPathCatalogueRequestEnvelope,
    role: str,
) -> str:
    if role == "identity_materialization":
        if request.operation != "ensure_project_identity":
            raise BatchProjectError("identity replay request operation differs")
    elif role in {"primary_mif", "moving_reference"}:
        if (
            request.operation != "upsert_entry"
            or not isinstance(request.payload, QuPathEntryUpsertRequest)
            or request.payload.entry_metadata.get("mif_pipeline.image_role") != role
        ):
            raise BatchProjectError("entry replay request role differs")
    else:
        raise BatchProjectError("operation replay role is invalid")
    return _digest(
        {
            "role": role,
            "operation": request.operation,
            "project_identity_sha256": request.project_identity_sha256,
            "payload": _operation_payload_mapping(request.payload),
        }
    )


def _validate_operation_request_evidence_pair(
    request: QuPathCatalogueRequestEnvelope,
    evidence: QuPathCatalogueEvidenceEnvelope,
    role: str,
) -> None:
    """Authenticate one exact replay transport pair before checkpointing it."""
    if (
        evidence.operation != request.operation
        or evidence.request_envelope_sha256 != request.request_envelope_sha256
        or evidence.launch_nonce_sha256 != request.launch_nonce_sha256
        or evidence.post_save_project_snapshot.identity_record is None
        or evidence.post_save_project_snapshot.identity_record.project_identity_sha256
        != request.project_identity_sha256
    ):
        raise BatchProjectError("operation replay evidence binding differs")
    if role == "identity_materialization":
        materialization = evidence.materialization_attestation
        if (
            request.operation != "ensure_project_identity"
            or not isinstance(request.payload, QuPathIdentityInstallRequest)
            or evidence.entry is not None
            or materialization is None
            or materialization.project_identity_sha256 != request.project_identity_sha256
            or materialization.mutation_request_sha256 != request.mutation_request_sha256
            or materialization.origin_kind != request.payload.identity_record.origin_kind
            or materialization.origin_qpproj_sha256
            != request.payload.identity_record.origin_qpproj_sha256
            or materialization.post_save_qpproj_snapshot_sha256
            != evidence.post_save_project_snapshot.qpproj_sha256
        ):
            raise BatchProjectError("identity replay evidence differs from its request")
        return
    if role not in {"primary_mif", "moving_reference"}:
        raise BatchProjectError("operation replay role is invalid")
    entry = evidence.entry
    payload = request.payload
    if (
        request.operation != "upsert_entry"
        or not isinstance(payload, QuPathEntryUpsertRequest)
        or entry is None
        or evidence.materialization_attestation is not None
        or entry not in evidence.post_save_project_snapshot.entries
        or entry.entry_name != payload.entry_name
        or payload.source_uri not in entry.source_uris
        or any(entry.metadata.get(key) != value for key, value in payload.entry_metadata.items())
        or entry.metadata.get("mif_pipeline.image_role") != role
        or (
            payload.expected_project_entry_id is not None
            and ((payload.entry_mode == "reuse" and entry.project_entry_id != payload.expected_project_entry_id)
                 or (payload.entry_mode == "replace" and (entry.project_entry_id == payload.expected_project_entry_id
                     or any(item.project_entry_id == payload.expected_project_entry_id for item in evidence.post_save_project_snapshot.entries))))
        )
        or (
            payload.require_saved_state_attestation and entry.saved_state_attestation_sha256 is None
        )
    ):
        raise BatchProjectError("entry replay evidence differs from its request")


def _operation_payload_mapping(
    payload: QuPathIdentityInstallRequest | QuPathEntryUpsertRequest,
) -> dict[str, object]:
    if isinstance(payload, QuPathIdentityInstallRequest):
        return {
            "identity_record": payload.identity_record.to_mapping(),
            "create_if_missing": payload.create_if_missing,
        }
    if isinstance(payload, QuPathEntryUpsertRequest):
        return {
            "entry_mode": payload.entry_mode,
            "source_uri": payload.source_uri,
            "source_image_sha256": payload.source_image_sha256,
            "entry_name": payload.entry_name,
            "expected_project_entry_id": payload.expected_project_entry_id,
            "entry_metadata": dict(payload.entry_metadata),
            "require_saved_state_attestation": (payload.require_saved_state_attestation),
        }
    raise BatchProjectError("operation replay payload is invalid")


def _expected_operation_replay_plan(
    request: EnsureBatchEntriesRequest,
    *,
    lease: ProjectMutationLease,
    catalogue_store: BatchProjectCatalogueStore,
) -> BatchProjectOperationReplayPlan:
    if request.expected_catalogue_sha256 is None:
        current = None
    else:
        current = catalogue_store.load_generation(request.expected_catalogue_sha256)
        if current.catalogue_sha256 != request.expected_catalogue_sha256:
            raise BatchProjectError("operation replay catalogue predecessor differs")
    prior_by_role = {
        item.image_role: item
        for item in (() if current is None else current.entries)
        if item.sample_id == request.sample_id
    }
    specifications: list[
        tuple[
            str,
            str,
            QuPathIdentityInstallRequest | QuPathEntryUpsertRequest,
        ]
    ] = []
    if request.designation.authority_head.materialization_state == "reserved":
        specifications.append(
            (
                "identity_materialization",
                "ensure_project_identity",
                QuPathIdentityInstallRequest(request.designation.identity, True),
            )
        )
    if not request.moving_only:
        primary_payload = _upsert_payload(
            request,
            source_uri=request.primary_source_uri,
            source_sha256=request.primary_processed_image.sha256,
            role="primary_mif",
            entry_mode=_entry_upsert_mode(request, "primary_mif", prior_by_role),
            expected_project_entry_id=(
                None
                if "primary_mif" not in prior_by_role
                else prior_by_role["primary_mif"].project_entry_id
            ),
        )
        specifications.append(("primary_mif", "upsert_entry", primary_payload))
    if isinstance(request.moving_reference, PairedMovingReferenceAuthority):
        reference_payload = _upsert_payload(
            request,
            source_uri=request.moving_reference.canonical_uri,
            source_sha256=request.moving_reference.source_sha256,
            role="moving_reference",
            entry_mode=_entry_upsert_mode(request, "moving_reference", prior_by_role),
            expected_project_entry_id=(
                None
                if "moving_reference" not in prior_by_role
                else prior_by_role["moving_reference"].project_entry_id
            ),
        )
        specifications.append(("moving_reference", "upsert_entry", reference_payload))
    steps = tuple(
        BatchProjectOperationReplayStep.create(
            ordinal=ordinal,
            role=role,
            operation=operation,
            request_identity_sha256=_digest(
                {
                    "role": role,
                    "operation": operation,
                    "project_identity_sha256": (
                        request.designation.identity.project_identity_sha256
                    ),
                    "payload": _operation_payload_mapping(payload),
                }
            ),
        )
        for ordinal, (role, operation, payload) in enumerate(specifications)
    )
    return BatchProjectOperationReplayPlan.create(
        mutation_request_sha256=lease.mutation_request_sha256,
        mutation_authorization_sha256=lease.mutation_authorization_sha256,
        steps=steps,
    )


def _ensure_operation_replay_plan(
    request: EnsureBatchEntriesRequest,
    *,
    lease: ProjectMutationLease,
    catalogue_store: BatchProjectCatalogueStore,
    workspace_lease: object,
) -> BatchProjectOperationReplayPlan:
    expected = _expected_operation_replay_plan(
        request,
        lease=lease,
        catalogue_store=catalogue_store,
    )
    if catalogue_store.has_operation_replay_plan(lease.mutation_request_sha256):
        observed = catalogue_store.reopen_operation_replay_plan(
            lease.mutation_request_sha256,
            workspace_lease=workspace_lease,
        )
        if observed != expected:
            raise BatchProjectError("operation replay plan differs from request")
    else:
        catalogue_store.publish_operation_replay_plan(expected, workspace_lease=workspace_lease)
    catalogue_store.reopen_operation_checkpoints(expected, workspace_lease=workspace_lease)
    return expected


def _replayed_operation_pair(
    plan: BatchProjectOperationReplayPlan,
    step: BatchProjectOperationReplayStep,
    *,
    catalogue_store: BatchProjectCatalogueStore,
    workspace_lease: object,
    runtime: QuPathProjectOperations,
    lease: ProjectMutationLease,
) -> (
    tuple[
        QuPathCatalogueRequestEnvelope,
        QuPathCatalogueEvidenceEnvelope,
    ]
    | None
):
    """Reopen the next checkpoint or its original completed native exchange.

    A recovery lease authenticates the retained spawn, launch and drain before
    we publish the missing checkpoint. Unfinished or mismatched native records
    cannot authorize a new launch or become an accepted operation pair.
    """
    checkpoints = catalogue_store.reopen_operation_checkpoints(
        plan, workspace_lease=workspace_lease
    )
    if len(checkpoints) > step.ordinal:
        _checkpoint, request, evidence = checkpoints[step.ordinal]
        catalogue_store._retire_checkpointed_pending_evidence(
            request, evidence, workspace_lease=workspace_lease
        )
        return request, evidence
    if len(checkpoints) != step.ordinal:
        raise BatchProjectError("operation checkpoint prefix is incomplete")
    recovered = catalogue_store.recover_pending_operation_checkpoint(
        plan, step, workspace_lease=workspace_lease,
    )
    if recovered is not None or not isinstance(lease, ProjectMutationRecoveryLease):
        return recovered
    provider = lease._provider
    active = provider._validate_live_lease_authority(lease, require_project=False)
    inventory = lease._provider_inventory
    if inventory is None:
        raise BatchProjectError("completed operation lacks its artifact inventory")
    attempt = next((item for item in provider._persisted_runtime_spawn_attempts(
        active, inventory
    ) if item.role == step.role), None)
    if attempt is None:
        return None
    launch = provider._persisted_runtime_process_launch_for_nonce(
        active, inventory, launch_nonce_sha256=attempt.launch_nonce_sha256,
    )
    drain = None if launch is None else provider._persisted_process_drain_for_launch(
        active, attempt, launch,
    )
    if drain is None:
        raise BatchProjectError("completed operation lacks its original process drain")
    mapping = catalogue_store._read_canonical_mapping(
        catalogue_store.request_ref(attempt.request_envelope_sha256),
        "completed operation request", max_bytes=_RUNTIME_REQUEST_MAX_BYTES,
    )
    request = QuPathCatalogueRequestEnvelope.from_mapping(mapping)
    if (request.request_envelope_sha256 != attempt.request_envelope_sha256
            or request.launch_nonce_sha256 != attempt.launch_nonce_sha256
            or _runtime_request_identity(request, step.role) != step.request_identity_sha256):
        raise BatchProjectError("completed operation differs from its replay step")
    evidence = runtime.reopen_completed_catalogue_evidence(
        lease.designation.project_path, request, lease=lease, process_drain=drain,
    )
    _validate_operation_request_evidence_pair(request, evidence, step.role)
    catalogue_store.publish_operation_checkpoint(
        plan, step, request, evidence, workspace_lease=workspace_lease,
    )
    return request, evidence


def _runtime_upsert(
    request: EnsureBatchEntriesRequest,
    *,
    runtime: QuPathProjectOperations,
    lease: ProjectMutationLease,
    source_uri: str,
    source_sha256: str,
    role: Literal["primary_mif", "moving_reference"],
    entry_mode: Literal["reuse", "replace"],
    expected_project_entry_id: str | None,
    replay_plan: BatchProjectOperationReplayPlan,
    catalogue_store: BatchProjectCatalogueStore,
    workspace_lease: object,
) -> tuple[QuPathCatalogueRequestEnvelope, QuPathCatalogueEvidenceEnvelope]:
    step = next((item for item in replay_plan.steps if item.role == role), None)
    if step is None:
        raise BatchProjectError("operation replay plan omits an entry role")
    replayed = _replayed_operation_pair(
        replay_plan,
        step,
        catalogue_store=catalogue_store,
        workspace_lease=workspace_lease,
        runtime=runtime,
        lease=lease,
    )
    payload = _upsert_payload(
        request,
        source_uri=source_uri,
        source_sha256=source_sha256,
        role=role,
        entry_mode=entry_mode,
        expected_project_entry_id=expected_project_entry_id,
    )
    metadata = dict(payload.entry_metadata)
    if replayed is not None:
        envelope, evidence = replayed
        if envelope.payload != payload:
            raise BatchProjectError("checkpointed upsert payload differs")
        _validate_operation_request_evidence_pair(envelope, evidence, role)
        return envelope, evidence
    unlaunched = catalogue_store._reopen_unlaunched_operation_request(
        replay_plan,
        step,
        workspace_lease=workspace_lease,
    )
    if unlaunched is not None:
        if unlaunched.payload != payload:
            raise BatchProjectError("unlaunched upsert payload differs")
        lease.prove_runtime_request_unlaunched(unlaunched)
        discard_replay = getattr(runtime, "_discard_unlaunched_project_replay", None)
        if callable(discard_replay):
            discard_replay(unlaunched, lease=lease)
        catalogue_store._retire_unlaunched_operation_request(
            replay_plan,
            step,
            unlaunched,
            workspace_lease=workspace_lease,
        )
    launch = runtime.issue_project_mutation_launch(lease=lease)
    launch_nonce = _sha(getattr(launch, "launch_nonce_sha256", None), "launch_nonce_sha256")
    envelope = QuPathCatalogueRequestEnvelope.create(
        operation="upsert_entry",
        project_identity_sha256=request.designation.identity.project_identity_sha256,
        mutation_request_sha256=lease.mutation_request_sha256,
        mutation_authorization_sha256=lease.mutation_authorization_sha256,
        fencing_token_sha256=lease.fencing_token_sha256,
        launch_nonce_sha256=launch_nonce,
        payload=payload,
    )
    if _runtime_request_identity(envelope, role) != step.request_identity_sha256:
        raise BatchProjectError("runtime request identity differs from replay plan")
    catalogue_store.publish_runtime_request(envelope, workspace_lease=workspace_lease)
    evidence = runtime.upsert_entry(request.designation.project_path, envelope, lease=lease)
    if not isinstance(evidence, QuPathCatalogueEvidenceEnvelope):
        raise BatchProjectError("QuPath upsert evidence is not typed")
    _validate_operation_request_evidence_pair(envelope, evidence, role)
    if (
        source_uri not in evidence.entry.source_uris
        or any(evidence.entry.metadata.get(key) != value for key, value in metadata.items())
        or (
            expected_project_entry_id is not None
            and ((entry_mode == "reuse" and evidence.entry.project_entry_id != expected_project_entry_id)
                 or (entry_mode == "replace" and evidence.entry.project_entry_id == expected_project_entry_id))
        )
        or evidence.post_save_project_snapshot.identity_record != request.designation.identity
        or evidence.post_save_project_snapshot.project_path.resolve()
        != request.designation.project_path.resolve()
    ):
        raise BatchProjectError("QuPath upsert evidence disagrees with its request")
    catalogue_store.publish_operation_checkpoint(
        replay_plan,
        step,
        envelope,
        evidence,
        workspace_lease=workspace_lease,
    )
    return envelope, evidence


def _prepared_batch_entries(
    request: EnsureBatchEntriesRequest,
    *,
    catalogue: BatchProjectCatalogue,
    runtime_evidence: tuple[
        tuple[QuPathCatalogueRequestEnvelope, QuPathCatalogueEvidenceEnvelope],
        ...,
    ],
    catalogue_store: BatchProjectCatalogueStore,
    context: BatchNodeExecutionContext,
) -> PreparedBatchEntries:
    by_role = {
        item.image_role: item for item in catalogue.entries if item.sample_id == request.sample_id
    }
    primary = by_role.get("primary_mif")
    reference = by_role.get("moving_reference")
    if primary is None or (
        isinstance(request.moving_reference, PairedMovingReferenceAuthority)
        != (reference is not None)
    ):
        raise BatchProjectError("runtime replay catalogue roles differ")
    evidence_by_role = {}
    for envelope, evidence in runtime_evidence:
        if (
            envelope.operation != "upsert_entry"
            or evidence.operation != "upsert_entry"
            or evidence.request_envelope_sha256 != envelope.request_envelope_sha256
            or evidence.entry is None
        ):
            raise BatchProjectError("runtime replay evidence is invalid")
        role = envelope.payload.entry_metadata.get("mif_pipeline.image_role")
        if role in evidence_by_role:
            raise BatchProjectError("runtime replay has duplicate image roles")
        evidence_by_role[role] = (envelope, evidence)
    expected_roles = (set() if request.moving_only else {"primary_mif"}) | ({"moving_reference"} if reference is not None else set())
    if set(evidence_by_role) != expected_roles:
        raise BatchProjectError("runtime replay evidence roles differ")
    for role, entry in by_role.items():
        if role not in expected_roles:
            continue
        envelope, evidence = evidence_by_role[role]
        if (
            envelope.project_identity_sha256 != catalogue.project_identity_sha256
            or envelope.mutation_request_sha256 != catalogue.mutation_request_sha256
            or envelope.mutation_authorization_sha256 != catalogue.mutation_authorization_sha256
            or evidence.entry.project_entry_id != entry.project_entry_id
            or entry.canonical_uri not in evidence.entry.source_uris
            or evidence.post_save_project_snapshot.identity_record != request.designation.identity
            or evidence.post_save_project_snapshot.project_uri != request.designation.project_uri
        ):
            raise BatchProjectError("runtime replay evidence differs from catalogue")
    final_evidence = (
        evidence_by_role["moving_reference"][1]
        if reference is not None
        else evidence_by_role["primary_mif"][1]
    )
    if request.moving_only:
        _require_preserved_primary(request, catalogue, final_evidence.post_save_project_snapshot)
    project_path = request.designation.project_path
    if (
        not project_content_matches(project_path, catalogue.qpproj_snapshot_sha256)
        or final_evidence.post_save_project_snapshot.qpproj_sha256
        != catalogue.qpproj_snapshot_sha256
    ):
        raise BatchProjectError("runtime replay project bytes differ")
    output = _project_output_artifact(request, context, catalogue, primary, catalogue_store)
    raw = BatchRawNodeOutcome(
        context=context,
        stage_result=StageResult.completed(
            stage=context.adapter_spec.node_kind,
            slide_id=request.slide_id,
        ),
        execution_result=BatchNodeExecutionResult(
            context.authorization.authorization_sha256, (output,)
        ),
        execution_exchange_sha256=None,
    )
    ordered_runtime = tuple(
        evidence_by_role[role]
        for role in ("primary_mif", "moving_reference")
        if role in evidence_by_role
    )
    return PreparedBatchEntries(
        catalogue=catalogue,
        primary_entry=primary,
        moving_reference_entry=reference,
        post_save_project_snapshot=final_evidence.post_save_project_snapshot,
        runtime_evidence=ordered_runtime,
        primary_project_entry_artifact_ref=output,
        raw_outcome=raw,
    )


def _require_preserved_primary(request, catalogue, snapshot):
    """Verify the actual native snapshot preserved the primary saved hierarchy.

    The moving operation already hashes every saved entry in its result. Reuse
    those observations rather than reading primary qpdata again or fabricating
    a primary upsert result.
    """
    primary = request.preserved_primary
    observed = [entry for entry in snapshot.entries if entry.project_entry_id == primary.project_entry_id]
    selected = [entry for entry in catalogue.entries if entry.sample_id == request.sample_id
                and entry.image_role == "primary_mif"]
    if (len(observed) != 1 or len(selected) != 1
            or observed[0].saved_state_attestation_sha256 != primary.saved_state_attestation_sha256
            or primary.canonical_uri not in observed[0].source_uris
            or observed[0].metadata.get("mif_pipeline.source_image_sha256") != primary.image_sha256):
        raise BatchProjectError("Paired-image preparation changed the primary image or its saved objects.")
    expected = primary._unsigned_mapping()
    expected["paired_entry_id"] = selected[0].paired_entry_id
    if selected[0] != BatchProjectEntry.create(**expected):
        raise BatchProjectError("Paired-image publication changed the retained primary association.")


def _require_project_catalogue_entry_output_contract(
    context: BatchNodeExecutionContext,
) -> ArtifactIdentityContract:
    """Require the exact owner-issued output contract before any effects."""

    if not isinstance(context, BatchNodeExecutionContext):
        raise BatchProjectError("project upsert requires a typed execution context")
    expected = project_catalogue_entry_identity_contract()
    try:
        contracts = tuple(context.adapter_spec.output_identity_contracts)
    except (AttributeError, TypeError) as exc:
        raise BatchProjectError(
            "project upsert lacks its project catalogue entry identity contract"
        ) from exc
    matches = tuple(
        contract
        for contract in contracts
        if isinstance(contract, ArtifactIdentityContract)
        and contract.artifact_kind == "project_catalogue_entry"
    )
    if len(matches) != 1 or matches[0] != expected:
        raise BatchProjectError("project catalogue entry identity contract is not owner-issued")
    return expected


def _project_output_artifact(
    request: EnsureBatchEntriesRequest,
    context: BatchNodeExecutionContext,
    catalogue: BatchProjectCatalogue,
    primary: BatchProjectEntry,
    catalogue_store: BatchProjectCatalogueStore,
) -> BatchArtifactRef:
    contract = _require_project_catalogue_entry_output_contract(context)
    identity_values = project_catalogue_entry_owning_identity(
        catalogue,
        primary.project_entry_id,
    )
    payload = _canonical_bytes(catalogue.to_mapping())
    locator = catalogue_store._generation_locator(catalogue.catalogue_sha256)
    return BatchArtifactRef(
        artifact_id="project-entry-" + primary.entry_projection_sha256[:48],
        artifact_kind="project_catalogue_entry",
        schema_name="mif-batch-project-catalogue",
        schema_version=1,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=(context.authorization.authorization_sha256),
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=locator,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_count=len(payload),
        scope=ArtifactScope(
            request.validation_domain_id,
            request.sample_id,
            request.slide_id,
            primary.project_entry_id,
            (),
        ),
        owning_identity=identity_values,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PROJECT_CATALOGUE_ENTRY_PRODUCER_VERSION,
        runtime_versions={"mif_pipeline": _MIF_PIPELINE_BATCH_RUNTIME_VERSION},
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=request.primary_processed_image.configuration_bindings,
    )


def ensure_batch_entries(
    request: EnsureBatchEntriesRequest,
    *,
    runtime: QuPathProjectOperations,
    lease: ProjectMutationLease,
    catalogue_store: BatchProjectCatalogueStore,
    workspace_lease: object,
    context: BatchNodeExecutionContext,
) -> PreparedBatchEntries:
    """Mutate one sample's project entries without committing local/state authority."""
    _require_project_catalogue_entry_output_contract(context)
    catalogue_store._authorize(workspace_lease)
    if (
        not isinstance(request, EnsureBatchEntriesRequest)
        or not isinstance(lease, ProjectMutationLease)
        or not lease.is_live
        or lease.designation.identity != request.designation.identity
        or lease.mutation_authorization_sha256 != context.authorization.authorization_sha256
        or tuple(context.authorization.dispatch_binding.participant_ids) != (request.sample_id,)
    ):
        raise BatchProjectError("project upsert request lacks exact live authority")
    replay_plan = _ensure_operation_replay_plan(
        request,
        lease=lease,
        catalogue_store=catalogue_store,
        workspace_lease=workspace_lease,
    )
    current = catalogue_store.load_current()
    observed_catalogue = None if current is None else current.catalogue_sha256
    if observed_catalogue != request.expected_catalogue_sha256:
        if (
            current is None
            or current.predecessor_catalogue_sha256 != request.expected_catalogue_sha256
            or current.mutation_request_sha256 != lease.mutation_request_sha256
            or current.mutation_authorization_sha256 != lease.mutation_authorization_sha256
        ):
            raise BatchProjectCatalogueConflictError(
                "workspace-local catalogue predecessor changed"
            )
    global_replay_attestation = lease.current_mutation_attestation()
    if not (
        isinstance(global_replay_attestation, ProjectMutationAttestation)
        and global_replay_attestation.mutation_kind == "batch_catalogue_upsert"
        and global_replay_attestation.mutation_request_sha256 == lease.mutation_request_sha256
        and global_replay_attestation.mutation_authorization_sha256
        == lease.mutation_authorization_sha256
    ):
        global_replay_attestation = None
    if (
        catalogue_store.has_runtime_replay(lease.mutation_request_sha256)
        or global_replay_attestation is not None
    ):
        replayed_catalogue, replayed_evidence = catalogue_store.reopen_runtime_replay(
            lease.mutation_request_sha256,
            workspace_lease=workspace_lease,
        )
        expected_authority_predecessor = (
            lease.current_authority_head.authority_head_sha256
            if global_replay_attestation is None
            else global_replay_attestation.predecessor_authority_head_sha256
        )
        if (
            replayed_catalogue.predecessor_catalogue_sha256 != request.expected_catalogue_sha256
            or replayed_catalogue.project_authority_predecessor_head_sha256
            != expected_authority_predecessor
            or replayed_catalogue.project_identity_sha256
            != request.designation.identity.project_identity_sha256
            or replayed_catalogue.project_uri != request.designation.project_uri
            or replayed_catalogue.mutation_request_sha256 != lease.mutation_request_sha256
            or replayed_catalogue.mutation_authorization_sha256
            != lease.mutation_authorization_sha256
            or (
                global_replay_attestation is not None
                and (
                    replayed_catalogue.qpproj_snapshot_sha256
                    != global_replay_attestation.post_save_qpproj_snapshot_sha256
                    or replayed_catalogue.catalogue_sha256
                    != global_replay_attestation.workspace_catalogue_sha256
                )
            )
            or (
                current is not None
                and current.catalogue_sha256
                not in {
                    request.expected_catalogue_sha256,
                    replayed_catalogue.catalogue_sha256,
                }
            )
        ):
            raise BatchProjectCatalogueConflictError(
                "global replay authority differs from retained runtime result"
            )
        return _prepared_batch_entries(
            request,
            catalogue=replayed_catalogue,
            runtime_evidence=replayed_evidence,
            catalogue_store=catalogue_store,
            context=context,
        )
    prior_by_role = {
        item.image_role: item
        for item in (() if current is None else current.entries)
        if item.sample_id == request.sample_id
    }
    if lease.current_authority_head.materialization_state != "materialized":
        raise BatchProjectError(
            "project identity must be globally materialized before entry mutation"
        )
    primary_uri = request.primary_source_uri
    primary_runtime = None
    primary_evidence = None
    if not request.moving_only:
        primary_runtime = _runtime_upsert(
            request,
            runtime=runtime,
            lease=lease,
            source_uri=primary_uri,
            source_sha256=request.primary_processed_image.sha256,
            role="primary_mif",
            entry_mode=_entry_upsert_mode(request, "primary_mif", prior_by_role),
            expected_project_entry_id=(
                None
                if "primary_mif" not in prior_by_role
                else prior_by_role["primary_mif"].project_entry_id
            ),
            replay_plan=replay_plan,
            catalogue_store=catalogue_store,
            workspace_lease=workspace_lease,
        )
        primary_request, primary_evidence = primary_runtime
    reference_evidence = None
    if isinstance(request.moving_reference, PairedMovingReferenceAuthority):
        reference_runtime = _runtime_upsert(
            request,
            runtime=runtime,
            lease=lease,
            source_uri=request.moving_reference.canonical_uri,
            source_sha256=request.moving_reference.source_sha256,
            role="moving_reference",
            entry_mode=_entry_upsert_mode(request, "moving_reference", prior_by_role),
            expected_project_entry_id=(
                None
                if "moving_reference" not in prior_by_role
                else prior_by_role["moving_reference"].project_entry_id
            ),
            replay_plan=replay_plan,
            catalogue_store=catalogue_store,
            workspace_lease=workspace_lease,
        )
        _reference_request, reference_evidence = reference_runtime
    final_evidence = reference_evidence or primary_evidence
    primary_id = (request.preserved_primary.project_entry_id if request.moving_only
                  else primary_evidence.entry.project_entry_id)
    reference_id = None if reference_evidence is None else reference_evidence.entry.project_entry_id
    primary = BatchProjectEntry.create(
        project_entry_id=primary_id,
        sample_id=request.sample_id,
        slide_id=request.slide_id,
        validation_domain_id=request.validation_domain_id,
        image_role="primary_mif",
        reference_modality=None,
        paired_entry_id=reference_id,
        canonical_uri=primary_uri,
        image_sha256=request.primary_processed_image.sha256,
        saved_state_attestation_sha256=(request.preserved_primary.saved_state_attestation_sha256
            if request.moving_only else primary_evidence.entry.saved_state_attestation_sha256),
    )
    reference = None
    if reference_evidence is not None:
        reference = BatchProjectEntry.create(
            project_entry_id=reference_id,
            sample_id=request.sample_id,
            slide_id=request.slide_id,
            validation_domain_id=request.validation_domain_id,
            image_role="moving_reference",
            reference_modality=request.moving_reference.modality,
            paired_entry_id=primary_id,
            canonical_uri=request.moving_reference.canonical_uri,
            image_sha256=request.moving_reference.source_sha256,
            saved_state_attestation_sha256=(
                reference_evidence.entry.saved_state_attestation_sha256
            ),
        )
    retained = tuple(
        item
        for item in (() if current is None else current.entries)
        if item.sample_id != request.sample_id
    )
    entries = (*retained, primary, *((reference,) if reference is not None else ()))
    catalogue = BatchProjectCatalogue.create(
        batch_id=request.batch_id,
        project_identity_sha256=request.designation.identity.project_identity_sha256,
        project_uri=request.designation.project_uri,
        project_authority_predecessor_head_sha256=(
            lease.current_authority_head.authority_head_sha256
        ),
        qpproj_snapshot_sha256=(final_evidence.post_save_project_snapshot.qpproj_sha256),
        predecessor_catalogue_sha256=request.expected_catalogue_sha256,
        mutation_request_sha256=lease.mutation_request_sha256,
        mutation_authorization_sha256=lease.mutation_authorization_sha256,
        entries=entries,
    )
    return _prepared_batch_entries(
        request,
        catalogue=catalogue,
        runtime_evidence=(
            *((primary_runtime,) if primary_runtime is not None else ()),
            *((reference_runtime,) if reference_evidence is not None else ()),
        ),
        catalogue_store=catalogue_store,
        context=context,
    )


def materialize_project_identity(
    request: EnsureBatchEntriesRequest,
    *,
    runtime: QuPathProjectOperations,
    lease: ProjectMutationLease,
    catalogue_store: BatchProjectCatalogueStore,
    workspace_lease: object,
) -> None:
    """Publish identity evidence and its global head before any entry write."""

    catalogue_store._authorize(workspace_lease)
    replay_plan = _ensure_operation_replay_plan(
        request,
        lease=lease,
        catalogue_store=catalogue_store,
        workspace_lease=workspace_lease,
    )
    step = next(
        (item for item in replay_plan.steps if item.role == "identity_materialization"),
        None,
    )
    if step is None:
        if lease.current_authority_head.materialization_state != "materialized":
            raise BatchProjectError("materialized replay plan disagrees with project authority")
        return
    replayed = _replayed_operation_pair(
        replay_plan,
        step,
        catalogue_store=catalogue_store,
        workspace_lease=workspace_lease,
        runtime=runtime,
        lease=lease,
    )
    completed_now = replayed is None
    if completed_now:
        if lease.current_authority_head.materialization_state == "materialized":
            raise BatchProjectError("materialized project lacks its operation checkpoint")
        unlaunched = catalogue_store._reopen_unlaunched_operation_request(
            replay_plan,
            step,
            workspace_lease=workspace_lease,
        )
        if unlaunched is not None:
            expected_payload = QuPathIdentityInstallRequest(request.designation.identity, True)
            if unlaunched.payload != expected_payload:
                raise BatchProjectError("unlaunched identity request payload differs")
            lease.prove_runtime_request_unlaunched(unlaunched)
            discard_replay = getattr(runtime, "_discard_unlaunched_project_replay", None)
            if callable(discard_replay):
                discard_replay(unlaunched, lease=lease)
            catalogue_store._retire_unlaunched_operation_request(
                replay_plan,
                step,
                unlaunched,
                workspace_lease=workspace_lease,
            )
        launch = runtime.issue_project_mutation_launch(lease=lease)
        launch_nonce = _sha(getattr(launch, "launch_nonce_sha256", None), "launch_nonce_sha256")
        identity_request = QuPathCatalogueRequestEnvelope.create(
            operation="ensure_project_identity",
            project_identity_sha256=(request.designation.identity.project_identity_sha256),
            mutation_request_sha256=lease.mutation_request_sha256,
            mutation_authorization_sha256=lease.mutation_authorization_sha256,
            fencing_token_sha256=lease.fencing_token_sha256,
            launch_nonce_sha256=launch_nonce,
            payload=QuPathIdentityInstallRequest(request.designation.identity, True),
        )
        if _runtime_request_identity(identity_request, step.role) != step.request_identity_sha256:
            raise BatchProjectError("identity runtime request differs from replay plan")
        catalogue_store.publish_runtime_request(identity_request, workspace_lease=workspace_lease)
        evidence = runtime.ensure_project_identity(
            request.designation.project_path,
            identity_request,
            lease=lease,
        )
    else:
        identity_request, evidence = replayed
    if not isinstance(evidence, QuPathCatalogueEvidenceEnvelope):
        raise BatchProjectError("QuPath identity materialization evidence is not typed")
    _validate_operation_request_evidence_pair(identity_request, evidence, step.role)
    if evidence.post_save_project_snapshot.identity_record != request.designation.identity:
        raise BatchProjectError("QuPath identity materialization evidence disagrees")
    if completed_now:
        catalogue_store.publish_operation_checkpoint(
            replay_plan,
            step,
            identity_request,
            evidence,
            workspace_lease=workspace_lease,
        )
    if lease.current_authority_head.materialization_state == "reserved":
        lease.materialize_identity(evidence.materialization_attestation)


def execute_project_upsert_stage(
    context: BatchNodeExecutionContext,
    request: EnsureBatchEntriesRequest,
    *,
    runtime: QuPathProjectOperations,
    catalogue_store: BatchProjectCatalogueStore,
    state_store: BatchStateStore,
    workspace_lease: object,
    project_lease: ProjectMutationLease,
    terminal_committer: BatchTerminalCommitter,
) -> ScheduledNodeResult:
    """Commit one upsert's catalogue, project material, and terminal authority."""
    _require_project_catalogue_entry_output_contract(context)
    head = state_store.load_head()
    materialize_project_identity(
        request,
        runtime=runtime,
        lease=project_lease,
        catalogue_store=catalogue_store,
        workspace_lease=workspace_lease,
    )
    prepared_entries = ensure_batch_entries(
        request,
        runtime=runtime,
        lease=project_lease,
        catalogue_store=catalogue_store,
        workspace_lease=workspace_lease,
        context=context,
    )
    prepared_result = state_store.prepare_project_mutation_node_result(
        context,
        prepared_entries.raw_outcome.stage_result,
        prepared_entries.raw_outcome.execution_result,
        execution_exchange_sha256=None,
        project_lease=project_lease,
        expected_head_sha256=head.sha256,
        lease=workspace_lease,
    )
    catalogue_store.publish_runtime_replay(
        prepared_entries.catalogue,
        prepared_entries.runtime_evidence,
        workspace_lease=workspace_lease,
    )
    _require_exact_runtime_replay(
        prepared_entries,
        catalogue_store=catalogue_store,
        workspace_lease=workspace_lease,
        require_committed_catalogue=False,
    )
    catalogue_store.stage_successor_generation(
        prepared_entries.catalogue,
        expected_catalogue_sha256=request.expected_catalogue_sha256,
        workspace_lease=workspace_lease,
    )
    state_store.stage_project_terminal_attempt(
        context,
        prepared_result,
        project_lease=project_lease,
        expected_head_sha256=head.sha256,
        lease=workspace_lease,
    )
    attestation = project_lease.commit_catalogue_authority(
        prepared_entries.catalogue,
        prepared_entries.runtime_evidence,
    )
    entry_ids = dict(head.sample_project_entry_id_by_sample)
    projections = dict(head.sample_project_entry_projection_sha256_by_sample)
    entry_ids[request.sample_id] = prepared_entries.primary_entry.project_entry_id
    projections[request.sample_id] = prepared_entries.primary_entry.entry_projection_sha256
    material_update = BatchMaterialAuthorityUpdate(
        source_sha256_by_sample=dict(head.source_sha256_by_sample),
        resolved_config_sha256_by_sample=dict(head.resolved_config_sha256_by_sample),
        project_identity_sha256=(project_lease.designation.identity.project_identity_sha256),
        project_authority_head_sha256=(project_lease.current_authority_head.authority_head_sha256),
        project_materialization_state="materialized",
        project_qpproj_snapshot_sha256=(
            project_lease.current_authority_head.qpproj_snapshot_sha256
        ),
        project_mutation_attestation_sha256=(
            project_lease.current_authority_head.latest_mutation_attestation_sha256
        ),
        project_catalogue_sha256=prepared_entries.catalogue.catalogue_sha256,
        sample_project_entry_id_by_sample=entry_ids,
        sample_project_entry_projection_sha256_by_sample=projections,
        accepted_domain_revision_sha256_by_id=dict(head.accepted_domain_revision_sha256_by_id),
        domain_set_sha256_by_sample=dict(head.domain_set_sha256_by_sample),
        checkpoint_descriptor_sha256_by_node=dict(head.checkpoint_descriptor_sha256_by_node),
        external_artifact_ref_sha256_by_key={
            key: key
            for key in state_store._load_plan_snapshot(
                head.active_plan_sha256
            ).external_input_sha256s
        },
    )
    material = state_store.prepare_project_material_successor(
        material_update,
        context=context,
        prepared_result=prepared_result,
        project_lease=project_lease,
        expected_head_sha256=head.sha256,
        lease=workspace_lease,
    )
    catalogue_store.commit_successor(
        prepared_entries.catalogue,
        expected_catalogue_sha256=request.expected_catalogue_sha256,
        workspace_lease=workspace_lease,
        authority_attestation=attestation,
    )
    scheduled, receipt = terminal_committer.commit_project_mutation(prepared_result, material)
    _require_exact_runtime_replay(
        prepared_entries,
        catalogue_store=catalogue_store,
        workspace_lease=workspace_lease,
        require_committed_catalogue=True,
    )
    project_lease.complete(receipt)
    return scheduled


def _streaming_sha256(path: Path) -> str:
    """Hash one retained image without materializing it in memory."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BatchProjectError("moving reference is unavailable") from exc
    return digest.hexdigest()


def build_current_project_upsert_request(
    context: BatchNodeExecutionContext,
    *,
    state_store: BatchStateStore,
    project_provider: ProjectMutationLeaseProvider,
    workspace_lease: object,
) -> EnsureBatchEntriesRequest:
    """Author one project-upsert request from exact current owner authority."""

    if (
        type(context) is not BatchNodeExecutionContext
        or type(state_store) is not BatchStateStore
        or type(project_provider) is not ProjectMutationLeaseProvider
        or getattr(state_store, "_project_mutation_provider", project_provider)
        is not project_provider
    ):
        raise BatchProjectError("project-upsert request composition is invalid")
    try:
        current = state_store.authenticate_current_execution_context(context)
        snapshot = state_store.reopen_execution_snapshot(
            expected_head_sha256=current.state_head_sha256
        )
    except Exception as exc:
        raise BatchProjectError("project-upsert execution authority is unavailable") from exc
    moving_only = current.node_kind == "paired_image_preparation"
    if (
        current.node_kind not in {"project_upsert", "paired_image_preparation"}
        or current.node_scope != "shared_project"
        or len(current.participant_ids) != 1
        or len(context.inputs) != (3 if moving_only else 2)
    ):
        raise BatchProjectError("project-upsert requires one participant and its accepted image and project inputs")
    inputs_by_kind = {item.artifact_kind: item for item in context.inputs}
    expected_inputs = {"analysis_frame", "processed_image"} | ({"project_catalogue_entry"} if moving_only else set())
    if set(inputs_by_kind) != expected_inputs:
        raise BatchProjectError("project-upsert requires analysis-frame and processed-image inputs")
    if tuple(sorted(item.artifact_ref_sha256 for item in context.inputs)) != (
        current.input_artifact_ref_sha256s
    ):
        raise BatchProjectError("project-upsert context input authority changed")
    frame = inputs_by_kind["analysis_frame"]
    processed = inputs_by_kind["processed_image"]
    try:
        state_store.authenticate_available_artifact(frame)
        state_store.authenticate_available_artifact(processed)
        if moving_only:
            state_store.authenticate_available_artifact(inputs_by_kind["project_catalogue_entry"])
    except Exception as exc:
        raise BatchProjectError("project-upsert input artifact is not current") from exc
    sample_id = current.participant_ids[0]
    samples = tuple(
        item for item in snapshot.manifest.samples if item.enabled and item.sample_id == sample_id
    )
    if len(samples) != 1:
        raise BatchProjectError("project-upsert participant lacks one enabled manifest sample")
    sample = samples[0]
    expected_scope = (
        sample.validation_domain_id,
        sample.sample_id,
        sample.slide_id,
    )
    if current.validation_domain_id != sample.validation_domain_id or any(
        (
            artifact.scope.validation_domain_id,
            artifact.scope.sample_id,
            artifact.scope.slide_id,
        )
        != expected_scope
        for artifact in (frame, processed)
    ):
        raise BatchProjectError("project-upsert inputs differ from the manifest participant scope")
    head = snapshot.head
    try:
        designation = project_provider.resolve_current_designation(
            Path(head.project_path),
            expected_project_identity_sha256=head.project_identity_sha256,
            expected_authority_head_sha256=(head.project_authority_head_sha256),
            pending_upsert_authorization_sha256=context.authorization.authorization_sha256,
        )
    except Exception as exc:
        raise BatchProjectError("project-upsert designation is unavailable") from exc
    if (
        designation.project_uri != canonical_project_uri(Path(head.project_path))
        or designation.identity.project_identity_sha256 != head.project_identity_sha256
        or designation.authority_head.authority_head_sha256 != head.project_authority_head_sha256
        or designation.authority_head.materialization_state != head.project_materialization_state
        or designation.authority_head.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
        or context.project_identity_sha256 != head.project_identity_sha256
        or context.project_authority_head_sha256 != head.project_authority_head_sha256
        or context.project_qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
    ):
        raise BatchProjectError("project-upsert designation differs from current state")
    catalogue_store = state_store.project_catalogue_store(workspace_lease)
    current_catalogue = catalogue_store.load_current()
    if head.project_catalogue_sha256 is None:
        if current_catalogue is not None:
            raise BatchProjectError("project-upsert catalogue predecessor differs from state")
        expected_catalogue_sha256 = None
    else:
        if (
            current_catalogue is None
            or current_catalogue.catalogue_sha256 != head.project_catalogue_sha256
            or current_catalogue.project_identity_sha256 != head.project_identity_sha256
            or current_catalogue.project_uri != designation.project_uri
        ):
            raise BatchProjectError("project-upsert catalogue predecessor differs from state")
        expected_catalogue_sha256 = current_catalogue.catalogue_sha256
    primary_prior = None if current_catalogue is None else next((entry for entry in current_catalogue.entries
        if entry.sample_id == sample_id and entry.image_role == "primary_mif"), None)
    if primary_prior is not None and primary_prior.canonical_uri != processed.locator.resolve().as_uri() + "/":
        inspection = next((node for node in snapshot.plan.nodes if node.kind == "inspection" and node.participant_ids == (sample_id,)), None)
        if inspection is None or inspection.settled_review_revision is None:
            raise BatchProjectError("Replacing a saved image requires its admitted source correction.")
    preserved_primary = None
    if moving_only:
        selected_node = next(node for node in snapshot.plan.nodes if node.node_id == context.node_id)
        moving_reference = selected_node.paired_reference
        _original_catalogue, original_primary = reopen_project_catalogue_entry_artifact(
            inputs_by_kind["project_catalogue_entry"])
        if (moving_reference is None or primary_prior is None
                or (primary_prior.project_entry_id, primary_prior.canonical_uri, primary_prior.image_sha256)
                != (original_primary.project_entry_id, original_primary.canonical_uri, original_primary.image_sha256)
                or current.project_entry_ids != (primary_prior.project_entry_id,)):
            raise BatchProjectError("Paired-image preparation must retain its accepted primary image.")
        if (moving_reference.source_path.is_symlink()
                or _streaming_sha256(moving_reference.source_path) != moving_reference.source_sha256):
            raise BatchProjectError("The paired image changed after inspection. Inspect it again before applying this selection.")
        old_moving = next((entry for entry in current_catalogue.entries
            if entry.sample_id == sample_id and entry.image_role == "moving_reference"), None)
        if (old_moving is not None and old_moving.canonical_uri == moving_reference.canonical_uri
                and old_moving.image_sha256 != moving_reference.source_sha256):
            raise BatchProjectError("The previous paired image changed in place. Choose a different file to preserve earlier results.")
        current_saved = state_store.current_project_entry_saved_state_sha256_by_id(
            expected_head_sha256=head.sha256, lease=workspace_lease)
        preserved_primary = BatchProjectEntry.create(**{
            **primary_prior._unsigned_mapping(),
            "saved_state_attestation_sha256": current_saved[primary_prior.project_entry_id],
        })
    elif sample.reference is None:
        moving_reference: MovingReferenceAuthority = NoMovingReferenceAuthority.create()
    else:
        reference = sample.reference
        moving_reference = PairedMovingReferenceAuthority.create(
            reference_id=reference.reference_id,
            source_path=reference.source_path,
            canonical_uri=reference.source_path.as_uri(),
            source_sha256=_streaming_sha256(reference.source_path),
            modality=reference.modality,
            source_image_id=reference.source_image_id,
            source_metadata=reference.source_metadata,
        )
    request = EnsureBatchEntriesRequest.create(
        batch_id=head.batch_id,
        designation=designation,
        sample_id=sample.sample_id,
        slide_id=sample.slide_id,
        validation_domain_id=sample.validation_domain_id,
        primary_processed_image=processed,
        moving_reference=moving_reference,
        expected_catalogue_sha256=expected_catalogue_sha256,
        preserved_primary=preserved_primary,
    )
    try:
        final_snapshot = state_store.reopen_execution_snapshot(
            expected_head_sha256=current.state_head_sha256
        )
    except Exception as exc:
        raise BatchProjectError("project-upsert state changed before request publication") from exc
    if final_snapshot.head.sha256 != head.sha256:
        raise BatchProjectError("project-upsert state changed before request publication")
    return request


def _require_exact_runtime_replay(
    prepared_entries: PreparedBatchEntries,
    *,
    catalogue_store: BatchProjectCatalogueStore,
    workspace_lease: object,
    require_committed_catalogue: bool,
) -> None:
    """Authenticate the closed replay graph at both completion boundaries."""

    replayed_catalogue, replayed_evidence = catalogue_store.reopen_runtime_replay(
        prepared_entries.catalogue.mutation_request_sha256,
        workspace_lease=workspace_lease,
    )
    if (
        replayed_catalogue != prepared_entries.catalogue
        or replayed_evidence != prepared_entries.runtime_evidence
    ):
        raise BatchProjectError("durable runtime replay differs from prepared result")
    if not require_committed_catalogue:
        return
    if (
        catalogue_store.load_generation(prepared_entries.catalogue.catalogue_sha256)
        != prepared_entries.catalogue
        or catalogue_store.load_current() != prepared_entries.catalogue
    ):
        raise BatchProjectError("committed catalogue differs from its authenticated replay")


def _project_upsert_artifact_inventory(
    request: EnsureBatchEntriesRequest,
    *,
    request_capability: ProjectMutationRequestCapability,
    state_store: BatchStateStore,
    project_provider: ProjectMutationLeaseProvider,
    catalogue_store: BatchProjectCatalogueStore,
) -> tuple[
    tuple[ProjectMutationArtifactTarget, ...],
    tuple[ProjectMutationArtifactTarget, ...],
]:
    """Build the closed preimage of every Task9 project-upsert write surface."""

    def predecessor(path: Path) -> str | None:
        if path.is_symlink():
            raise BatchProjectError("project mutation target is a symbolic link")
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

    project = request.designation.project_path
    identity = request.designation.identity.project_identity_sha256
    authority_root = project_provider.authority_root
    active_path = (
        authority_root
        / "active-by-locator"
        / hashlib.sha256(request.designation.project_uri.encode("utf-8")).hexdigest()
        / "active.json"
    )
    locator_identity_path = (
        authority_root
        / "locator-identities"
        / (hashlib.sha256(request.designation.project_uri.encode("utf-8")).hexdigest() + ".json")
    )
    request_path = request_capability.persisted_path
    identity_root = authority_root / "projects" / identity
    replay_path = (
        catalogue_store.root / "project-runtime-replays" / f"{request.request_sha256}.json"
    )
    replay_root = catalogue_store.root / "project-operation-replays" / request.request_sha256
    operation_roles = (
        *(
            ("identity_materialization",)
            if (request.designation.authority_head.materialization_state == "reserved")
            else ()
        ),
        *(("primary_mif",) if not request.moving_only else ()),
        *(
            ("moving_reference",)
            if isinstance(request.moving_reference, PairedMovingReferenceAuthority)
            else ()
        ),
    )
    current_path = catalogue_store.root / "project-current.json"
    current_catalogue = catalogue_store.load_current()
    if catalogue_store.authority_layout_sha256 is None:
        current_catalogue_sha256 = predecessor(current_path)
    else:
        current_catalogue_sha256 = (
            None
            if current_catalogue is None
            else hashlib.sha256(_canonical_bytes(current_catalogue.to_mapping())).hexdigest()
        )
    state_head_path = state_store.layout.state_head_path
    prior_by_role = {entry.image_role: entry for entry in (() if current_catalogue is None else current_catalogue.entries)
                     if entry.sample_id == request.sample_id}
    history_path = source_project_history_path(project, request.request_sha256)
    history_specs = ()
    replaced_role = "moving_reference" if request.moving_only else "primary_mif"
    if _entry_upsert_mode(request, replaced_role, prior_by_role) == "replace":
        old_data = project.parent / "data" / prior_by_role[replaced_role].project_entry_id / "data.qpdata"
        history_specs = (
            ("source_project_history", "exact_uri", project.parent, history_path.name, predecessor(history_path)),
            ("source_entry_history", "exact_uri", project.parent,
             old_data.relative_to(project.parent).as_posix(), predecessor(old_data)))
    static_specs = (
        *history_specs,
        ("qpproj", "exact_uri", project.parent, project.name, predecessor(project)),
        (
            "identity_sidecar",
            "exact_uri",
            project.parent,
            f"{project.name}.mif-project-identity.json",
            predecessor(Path(f"{project}.mif-project-identity.json")),
        ),
        (
            "identity_metadata",
            "project_metadata_key",
            project.parent,
            "mif_pipeline.project_identity_record_v1",
            hashlib.sha256(_canonical_bytes(request.designation.identity.to_mapping())).hexdigest(),
        ),
        (
            "global_identity_record",
            "authority_identity_relative_from_provider",
            authority_root,
            "identity.json",
            predecessor(identity_root / "identity.json"),
        ),
        (
            "authority_head",
            "authority_identity_relative_from_provider",
            authority_root,
            "authority-head.json",
            predecessor(identity_root / "authority-head.json"),
        ),
        (
            "artifact_resolution",
            "authority_attestation_from_verified_evidence",
            authority_root,
            "planned",
            None,
        ),
        (
            "artifact_resolution",
            "authority_attestation_from_verified_evidence",
            authority_root,
            "final",
            None,
        ),
        *(
            (
                (
                    "materialization_attestation",
                    "authority_attestation_from_verified_evidence",
                    authority_root,
                    "materialization",
                    None,
                ),
            )
            if request.designation.authority_head.materialization_state == "reserved"
            else ()
        ),
        (
            "mutation_attestation",
            "authority_attestation_from_verified_evidence",
            authority_root,
            "batch_catalogue_upsert",
            None,
        ),
        *(
            (
                "process_spawn_attempt",
                "authority_attestation_from_verified_evidence",
                authority_root,
                f"spawn_attempt:{role}",
                None,
            )
            for role in operation_roles
        ),
        (
            "active_lease_record",
            "exact_uri",
            active_path.parent,
            active_path.name,
            predecessor(active_path),
        ),
        (
            "locator_identity",
            "exact_uri",
            locator_identity_path.parent,
            locator_identity_path.name,
            predecessor(locator_identity_path),
        ),
        (
            "catalogue_request_or_evidence",
            "exact_uri",
            request_path.parent,
            request_path.name,
            predecessor(request_path),
        ),
        (
            "catalogue_request_or_evidence",
            "exact_uri",
            replay_path.parent,
            replay_path.name,
            predecessor(replay_path),
        ),
        (
            "catalogue_request_or_evidence",
            "exact_uri",
            replay_root,
            "plan.json",
            predecessor(replay_root / "plan.json"),
        ),
        *(
            (
                "catalogue_request_or_evidence",
                "exact_uri",
                replay_root / "checkpoints",
                f"{ordinal:03d}-{role}.json",
                predecessor(replay_root / "checkpoints" / f"{ordinal:03d}-{role}.json"),
            )
            for ordinal, role in enumerate(operation_roles)
        ),
        *(
            (
                "catalogue_request_or_evidence",
                "authority_attestation_from_verified_evidence",
                catalogue_store.root,
                f"{record_kind}:{role}",
                None,
            )
            for role in operation_roles
            for record_kind in ("runtime_request", "runtime_evidence")
        ),
        (
            "catalogue_generation",
            "authority_attestation_from_verified_evidence",
            catalogue_store.root,
            "candidate",
            None,
        ),
        (
            "catalogue_current",
            "exact_uri",
            current_path.parent,
            current_path.name,
            current_catalogue_sha256,
        ),
        (
            "batch_state_head",
            "exact_uri",
            state_head_path.parent,
            state_head_path.name,
            predecessor(state_head_path),
        ),
        (
            "terminal_attempt",
            "authority_attestation_from_verified_evidence",
            state_store.layout.attempts,
            "terminal",
            None,
        ),
    )
    static = tuple(
        ProjectMutationArtifactTarget.create(
            target_kind=kind,
            resolution_kind=resolution,
            owner_root_uri=owner.resolve().as_uri(),
            relative_locator_or_metadata_key=relative,
            predecessor_sha256=prior,
        )
        for kind, resolution, owner, relative, prior in static_specs
    )
    dynamic = tuple(
        ProjectMutationArtifactTarget.create(
            target_kind=kind,
            resolution_kind="entry_id_from_runtime_evidence",
            owner_root_uri=project.parent.resolve().as_uri(),
            relative_locator_or_metadata_key=role,
            predecessor_sha256=None,
        )
        for role in (
            *(("primary_mif",) if not request.moving_only else ()),
            *(
                ("moving_reference",)
                if isinstance(request.moving_reference, PairedMovingReferenceAuthority)
                else ()
            ),
        )
        for kind in (
            ("entry_qpdata", "entry_hierarchy_tree")
            if role == "primary_mif"
            else ("entry_hierarchy_tree",)
        )
    )
    return static, dynamic


def _recovered_abnormal_abort_evidence(
    request: EnsureBatchEntriesRequest,
    *,
    project_lease: ProjectMutationLease,
    catalogue_store: BatchProjectCatalogueStore,
    project_provider: ProjectMutationLeaseProvider,
    workspace_lease: object,
) -> object | None:
    """Classify one exact abnormal recovered launch without relaunching it."""

    if not isinstance(project_lease, ProjectMutationRecoveryLease):
        return None
    process_drain = project_lease._recovered_process_drain
    if (
        process_drain is None
        or process_drain.drain_evidence is None
        or process_drain.drain_evidence.termination_reason == "normal_exit"
    ):
        return None
    replay_plan = catalogue_store.reopen_operation_replay_plan(
        project_lease.mutation_request_sha256,
        workspace_lease=workspace_lease,
    )
    expected_plan = _expected_operation_replay_plan(
        request,
        lease=project_lease,
        catalogue_store=catalogue_store,
    )
    if replay_plan != expected_plan:
        raise BatchProjectError("recovered abnormal process replay plan differs")
    interrupted_nonce = process_drain.launch_nonce_sha256
    interrupted_pairs = tuple(
        (runtime_request, runtime_evidence)
        for _checkpoint, runtime_request, runtime_evidence in (
            catalogue_store.reopen_operation_checkpoints(
                replay_plan,
                workspace_lease=workspace_lease,
            )
        )
        if runtime_request.launch_nonce_sha256 == interrupted_nonce
        and runtime_evidence.launch_nonce_sha256 == interrupted_nonce
    )
    if len(interrupted_pairs) != 1:
        raise BatchProjectError("recovered abnormal process lacks one exact runtime checkpoint")
    return project_provider.issue_recovered_abort_evidence(
        project_lease,
        runtime_evidence=interrupted_pairs,
    )


def _checkpoint_and_classify_abnormal_runtime(
    operation_error: QuPathCatalogueOperationError,
    *,
    runtime: QuPathRunnerProjectOperations,
    request: EnsureBatchEntriesRequest,
    catalogue_store: BatchProjectCatalogueStore,
    state_store: BatchStateStore,
    project_provider: ProjectMutationLeaseProvider,
    request_capability: ProjectMutationRequestCapability,
    inventory_capability: object,
    acquisition: object,
    workspace_lease: object,
) -> object:
    """Checkpoint one complete abnormal child pair before classification."""

    try:
        runtime_request, runtime_evidence = runtime._authenticate_abnormal_operation_error(
            operation_error
        )
    except ProjectAuthorityError as exc:
        raise BatchProjectError("abnormal QuPath transport authority is invalid") from exc
    if (
        runtime_evidence is None
        or operation_error.evidence_ref is None
        or operation_error.process_start_token is None
        or operation_error.launch_evidence is None
        or operation_error.drain_evidence is None
        or operation_error.process_drain_sha256 is None
        or operation_error.drain_evidence.termination_reason
        not in {"nonzero_exit", "timeout", "cancelled"}
    ):
        raise BatchProjectError("abnormal QuPath transport is incomplete or ambiguous")
    plan = catalogue_store.reopen_operation_replay_plan(
        runtime_request.mutation_request_sha256,
        workspace_lease=workspace_lease,
    )
    matching_steps: list[BatchProjectOperationReplayStep] = []
    for candidate in plan.steps:
        if candidate.operation != runtime_request.operation:
            continue
        try:
            request_identity = _runtime_request_identity(runtime_request, candidate.role)
        except BatchProjectError:
            continue
        if candidate.request_identity_sha256 == request_identity:
            matching_steps.append(candidate)
    if (
        plan.mutation_request_sha256 != runtime_request.mutation_request_sha256
        or len(matching_steps) != 1
    ):
        raise BatchProjectError("abnormal QuPath transport has no exact replay step")
    step = matching_steps[0]
    completed = catalogue_store.reopen_operation_checkpoints(plan, workspace_lease=workspace_lease)
    if len(completed) != step.ordinal:
        raise BatchProjectError("abnormal QuPath checkpoint prefix is incomplete")
    _validate_operation_request_evidence_pair(runtime_request, runtime_evidence, step.role)
    catalogue_store.publish_operation_checkpoint(
        plan,
        step,
        runtime_request,
        runtime_evidence,
        workspace_lease=workspace_lease,
    )
    reopened = catalogue_store.reopen_operation_checkpoints(plan, workspace_lease=workspace_lease)
    if len(reopened) != step.ordinal + 1 or reopened[step.ordinal][1:] != (
        runtime_request,
        runtime_evidence,
    ):
        raise BatchProjectError("abnormal QuPath checkpoint changed after publication")

    recovery_session = project_provider.issue_acquisition_session(
        acquisition,
        request=request_capability,
        inventory=inventory_capability,
        durability_authority=state_store.project_mutation_durability_authority(workspace_lease),
    )
    stale_active = project_provider.recovery_active_record_sha256(recovery_session)
    if stale_active is None:
        raise BatchProjectError("checkpointed abnormal QuPath process has no recoverable lease")
    with project_provider.recover(
        recovery_session,
        stale_active_record_sha256=stale_active,
    ) as recovery_lease:
        abort_evidence = _recovered_abnormal_abort_evidence(
            request,
            project_lease=recovery_lease,
            catalogue_store=catalogue_store,
            project_provider=project_provider,
            workspace_lease=workspace_lease,
        )
        if abort_evidence is None:
            raise BatchProjectError("checkpointed abnormal QuPath process was not classifiable")
        recovery_lease.abort(abort_evidence)
        return abort_evidence


def recover_completed_project_upsert(
    *,
    state_store: BatchStateStore,
    workspace_lease: object,
) -> bool:
    """Close a committed image upsert whose lease outlived its Python process.

    Return whether a completed lease was recovered. The original request and
    native replay supply its saved catalogue; the existing terminal receipt
    closes the journal without repeating the image upsert or committing another result.
    Pending operations remain with their ordinary execution owner.
    """
    provider = state_store._project_mutation_provider
    if provider is None:
        return False
    head = state_store.load_head()
    active_records = provider._unresolved_active_records_for_project(
        project_uri=canonical_project_uri(Path(head.project_path)),
        project_identity_sha256=head.project_identity_sha256,
    )
    if not active_records:
        return False
    active = active_records[0][1]
    if active.operation != "batch_catalogue_upsert":
        return False
    authorization = state_store.load_authorization(active.mutation_authorization_sha256)
    if (
        head.node_status_by_id.get(authorization.node_id) is not StageStatus.COMPLETED
        or authorization.node_id in head.pending_authorization_sha256_by_node
    ):
        return False
    catalogue_store = state_store.project_catalogue_store(workspace_lease)
    request = catalogue_store.reopen_request(
        active.mutation_request_sha256,
        mutation_authorization_sha256=active.mutation_authorization_sha256,
        workspace_lease=workspace_lease,
    )
    inventory = state_store.reopen_project_mutation_artifact_inventory(
        active.artifact_inventory_sha256, lease=workspace_lease,
    )
    acquisition = state_store.reopen_project_mutation_acquisition_record(
        active.acquisition_record_sha256, lease=workspace_lease,
    )
    session = provider.issue_acquisition_session(
        acquisition, request=request, inventory=inventory,
        durability_authority=state_store.project_mutation_durability_authority(workspace_lease),
    )
    with provider.recover(session, stale_active_record_sha256=active.lease_record_sha256) as project_lease:
        catalogue, runtime_evidence = catalogue_store.reopen_runtime_replay(
            active.mutation_request_sha256, workspace_lease=workspace_lease,
        )
        if (
            catalogue_store.load_generation(catalogue.catalogue_sha256) != catalogue
            or catalogue_store.load_current() != catalogue
        ):
            raise BatchProjectError("committed catalogue differs from its authenticated replay")
        project_lease.commit_catalogue_authority(catalogue, runtime_evidence)
        receipt = state_store.reissue_project_terminal_durability_receipt(
            authorization, project_lease=project_lease, lease=workspace_lease,
        )
        project_lease.complete(receipt)
    return True


def execute_managed_project_upsert(
    context: BatchNodeExecutionContext,
    request: EnsureBatchEntriesRequest,
    *,
    runner: object,
    project_operations: QuPathRunnerProjectOperations | None = None,
    state_store: BatchStateStore,
    project_provider: ProjectMutationLeaseProvider,
    workspace_lease: object,
    terminal_committer: BatchTerminalCommitter,
) -> ScheduledNodeResult:
    """Execute or exactly recover one scheduler-authorized Task9 project upsert."""
    if (
        not isinstance(context, BatchNodeExecutionContext)
        or not isinstance(request, EnsureBatchEntriesRequest)
        or not isinstance(state_store, BatchStateStore)
        or not isinstance(project_provider, ProjectMutationLeaseProvider)
        or getattr(state_store, "_project_mutation_provider", None) is not project_provider
    ):
        raise BatchProjectError("managed project upsert composition is invalid")
    project_authority = project_provider.project_authority
    if (
        project_authority is None
        or project_provider._load_project_identity(
            request.designation.identity.project_identity_sha256
        ) != request.designation.identity
        or state_store.authority_layout_sha256 != project_authority.authority_layout_sha256
    ):
        raise BatchProjectError("managed project upsert authority layouts disagree")
    if project_operations is not None:
        try:
            state_store.authenticate_project_operations(project_operations, runner=runner)
        except Exception as exc:
            raise BatchProjectError("managed project operations binding is invalid") from exc
    try:
        state_store.validate_terminal_committer(
            terminal_committer,
            contexts=(context,),
            lease=workspace_lease,
        )
    except Exception as exc:
        raise BatchProjectError("managed project upsert terminal committer is invalid") from exc
    catalogue_store = state_store.project_catalogue_store(workspace_lease)
    if project_operations is None:
        project_operations = catalogue_store.bind_project_operations(runner=runner)
        try:
            state_store.authenticate_project_operations(project_operations, runner=runner)
        except Exception as exc:
            raise BatchProjectError("managed project operations binding is invalid") from exc
    current = catalogue_store.load_current()
    observed_catalogue = None if current is None else current.catalogue_sha256
    active = project_provider.discover_recoverable_active_record(
        project_uri=request.designation.project_uri,
        project_identity_sha256=(request.designation.identity.project_identity_sha256),
        mutation_request_sha256=request.request_sha256,
        mutation_authorization_sha256=(context.authorization.authorization_sha256),
        operation="batch_catalogue_upsert",
    )
    if active is None and observed_catalogue != request.expected_catalogue_sha256:
        raise BatchProjectCatalogueConflictError("workspace-local catalogue predecessor changed")
    if active is None:
        request_capability = catalogue_store.publish_request(
            request,
            mutation_authorization_sha256=(context.authorization.authorization_sha256),
            workspace_lease=workspace_lease,
        )
        static_targets, dynamic_targets = _project_upsert_artifact_inventory(
            request,
            request_capability=request_capability,
            state_store=state_store,
            project_provider=project_provider,
            catalogue_store=catalogue_store,
        )
        inventory = state_store.prepare_project_mutation_artifact_inventory(
            request=request_capability,
            expected_project_identity_sha256=(request.designation.identity.project_identity_sha256),
            static_targets=static_targets,
            dynamic_targets=dynamic_targets,
            lease=workspace_lease,
        )
        inventory_capability = state_store.publish_project_mutation_artifact_inventory(
            inventory, lease=workspace_lease
        )
        acquisition = state_store.publish_project_mutation_acquisition_record(
            request=request_capability,
            inventory=inventory_capability,
            operation="batch_catalogue_upsert",
            intent=("create" if not request.designation.project_path.exists() else "open_existing"),
            project_uri=request.designation.project_uri,
            expected_project_identity_sha256=(request.designation.identity.project_identity_sha256),
            expected_authority_head_sha256=(
                request.designation.authority_head.authority_head_sha256
            ),
            recovery_predecessor_acquisition_record_sha256=None,
            lease=workspace_lease,
        )
    else:
        request_capability = catalogue_store.reopen_request(
            active.mutation_request_sha256,
            mutation_authorization_sha256=(active.mutation_authorization_sha256),
            workspace_lease=workspace_lease,
        )
        inventory_capability = state_store.reopen_project_mutation_artifact_inventory(
            active.artifact_inventory_sha256,
            lease=workspace_lease,
        )
        acquisition = state_store.reopen_project_mutation_acquisition_record(
            active.acquisition_record_sha256,
            lease=workspace_lease,
        )
    session = project_provider.issue_acquisition_session(
        acquisition,
        request=request_capability,
        inventory=inventory_capability,
        durability_authority=state_store.project_mutation_durability_authority(workspace_lease),
    )
    stale_active = project_provider.recovery_active_record_sha256(session)
    lease_context = (
        project_provider.acquire(session)
        if stale_active is None
        else project_provider.recover(session, stale_active_record_sha256=stale_active)
    )
    runtime = project_operations
    try:
        with lease_context as project_lease:
            abort_evidence = _recovered_abnormal_abort_evidence(
                request,
                project_lease=project_lease,
                catalogue_store=catalogue_store,
                project_provider=project_provider,
                workspace_lease=workspace_lease,
            )
            if abort_evidence is not None:
                project_lease.abort(abort_evidence)
                raise BatchProjectError(
                    "recovered abnormal QuPath process was classified and aborted"
                )
            return execute_project_upsert_stage(
                context,
                request,
                runtime=runtime,
                catalogue_store=catalogue_store,
                state_store=state_store,
                workspace_lease=workspace_lease,
                project_lease=project_lease,
                terminal_committer=terminal_committer,
            )
    except QuPathCatalogueOperationError as operation_error:
        _checkpoint_and_classify_abnormal_runtime(
            operation_error,
            runtime=runtime,
            request=request,
            catalogue_store=catalogue_store,
            state_store=state_store,
            project_provider=project_provider,
            request_capability=request_capability,
            inventory_capability=inventory_capability,
            acquisition=acquisition,
            workspace_lease=workspace_lease,
        )
        raise BatchProjectError(
            "abnormal QuPath process was checkpointed, classified, and aborted"
        ) from operation_error


__all__ = [
    "BatchProjectCatalogue",
    "BatchProjectCatalogueConflictError",
    "BatchProjectCatalogueImportAuthority",
    "BatchProjectCatalogueStore",
    "BatchProjectEntry",
    "BatchProjectError",
    "CurrentProjectEntryChannelObservation",
    "BatchRegistrationProjectRequest",
    "BatchProjectOperationCheckpoint",
    "BatchProjectOperationReplayPlan",
    "BatchProjectOperationReplayStep",
    "BatchProjectRuntimeReplay",
    "BatchProjectRuntimeReplayPair",
    "EnsureBatchEntriesRequest",
    "MovingReferenceAuthority",
    "NoMovingReferenceAuthority",
    "PairedMovingReferenceAuthority",
    "PreparedBatchEntries",
    "deterministic_project_entry_name",
    "PROJECT_CATALOGUE_ENTRY_IDENTITY_FIELDS",
    "PROJECT_UPSERT_SCRIPT_ASSET",
    "build_current_project_upsert_request",
    "build_project_entry_pyramid_request",
    "ensure_batch_entries",
    "execute_project_upsert_stage",
    "execute_managed_project_upsert",
    "recover_completed_project_upsert",
    "project_catalogue_entry_identity_contract",
    "project_catalogue_entry_owning_identity",
    "reopen_project_catalogue_entry_artifact",
    "reopen_current_project_entry_channel_observation",
    "validate_project_catalogue_selection_witness",
    "validate_current_project_entry_pyramid_evidence",
]
