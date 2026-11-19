"""Strict provider-neutral values for an existing-project Warpy session.

This module defines canonical observations only. It never opens an image, discovers a
project, acquires a lease, or launches QuPath; those authorities remain with the project
provider and composed runner.

The `RegistrationRoleExecutionDescriptor` record carries `schema_name`, `schema_version`,
`role`, `disposition`, `failure_kind` and its remaining declared fields.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from .registration_transform import validate_transform_reader_request

from dataclasses import dataclass
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Mapping
from urllib.parse import urlsplit

from mif_pipeline.pipeline_core.authority_layout import PosixFileIdentityV2
from mif_pipeline.pipeline_core.durable_records import (
    DurableRecordError,
    canonical_json_bytes,
)
from mif_pipeline.pipeline_core.win32_file_authority import Win32FileIdentity

from .exchange import ExchangeContractError, validate_canonical_image_uri

if TYPE_CHECKING:
    from .catalogue import ProjectMutationLeaseProvider


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\Z")
_ENTRY_ROLES = ("fixed", "moving")
_MEMBER_KINDS = ("entry_qpdata", "entry_hierarchy_tree")


# ==============================================================================
#                              Registration session
# ==============================================================================


class RegistrationSessionContractError(ValueError):
    """An existing-project registration observation violates its contract."""


def _wire(value: object) -> object:
    if isinstance(value, (PosixFileIdentityV2, Win32FileIdentity)):
        return value.to_mapping()
    if hasattr(value, "to_mapping") and callable(value.to_mapping):
        return value.to_mapping()
    if type(value) is tuple:
        return [_wire(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _wire(item) for key, item in value.items()}
    return value


def _digest(value: object) -> str:
    try:
        return hashlib.sha256(canonical_json_bytes(_wire(value))).hexdigest()
    except Exception as exc:
        raise RegistrationSessionContractError(
            "registration-session material is not canonical JSON"
        ) from exc


def _closed(value: object, expected: frozenset[str], label: str) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or any(type(key) is not str for key in value)
        or frozenset(value) != expected
    ):
        raise RegistrationSessionContractError(f"{label} fields are not the current closed schema")
    return value


def _closed_factory_values(
    value: Mapping[str, object],
    *,
    record_fields: Mapping[str, object],
    derived_fields: frozenset[str],
    label: str,
) -> None:
    expected = frozenset(record_fields).difference(derived_fields)
    if frozenset(value) != expected:
        raise RegistrationSessionContractError(f"{label} factory fields are not closed")


def _sha(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise RegistrationSessionContractError(f"{label} must be one lowercase SHA-256")
    return value


def _safe_id(value: object, label: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise RegistrationSessionContractError(f"{label} must be one bounded safe identifier")
    return value


def _nonnegative(value: object, label: str) -> int:
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        raise RegistrationSessionContractError(f"{label} must be one bounded nonnegative integer")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0 or value > 2**63 - 1:
        raise RegistrationSessionContractError(f"{label} must be one bounded positive integer")
    return value


def _positive_hex(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise RegistrationSessionContractError(
            f"{label} must be canonical positive hexadecimal float text"
        )
    try:
        number = float.fromhex(value)
    except ValueError as exc:
        raise RegistrationSessionContractError(
            f"{label} must be canonical positive hexadecimal float text"
        ) from exc
    if number <= 0.0 or number == float("inf") or number != number:
        raise RegistrationSessionContractError(f"{label} must encode one positive finite value")
    if number.hex() != value:
        raise RegistrationSessionContractError(f"{label} must use canonical hexadecimal float text")
    return value


def _local_file_uri(value: object, label: str) -> str:
    try:
        uri = validate_canonical_image_uri(value)
    except ExchangeContractError as exc:
        raise RegistrationSessionContractError(
            f"{label} must be one canonical local file URI"
        ) from exc
    if urlsplit(uri).netloc:
        raise RegistrationSessionContractError(f"{label} must not use a network authority")
    return uri


def _uri_child(root_uri: str, name: str) -> str:
    root = _local_file_uri(root_uri, "entry_root_uri")
    if root.endswith("/"):
        raise RegistrationSessionContractError("entry_root_uri must not have a trailing separator")
    return f"{root}/{name}"


def _uri_parent(uri: str) -> str:
    parsed = urlsplit(_local_file_uri(uri, "member_uri"))
    parent_path = parsed.path.rsplit("/", 1)[0]
    return f"file://{parent_path}"


def _platform_identity_from_mapping(
    value: object,
) -> PosixFileIdentityV2 | Win32FileIdentity:
    if not isinstance(value, Mapping):
        raise RegistrationSessionContractError("retained platform identity must be an object")
    try:
        if value.get("kind") == "posix-file-identity/v2":
            return PosixFileIdentityV2.from_mapping(value)
        if value.get("kind") == "win32-file-identity/v1":
            return Win32FileIdentity.from_mapping(value)
    except Exception as exc:
        raise RegistrationSessionContractError("retained platform identity is invalid") from exc
    raise RegistrationSessionContractError("retained platform identity kind is unsupported")


@dataclass(frozen=True, slots=True)
class RetainedRegistrationFileIdentity:
    """Compact provider observation; it contains no file bytes or WSI hash."""

    canonical_uri: str
    platform_identity: PosixFileIdentityV2 | Win32FileIdentity
    identity_sha256: str

    def __post_init__(self) -> None:
        _local_file_uri(self.canonical_uri, "canonical_uri")
        if type(self.platform_identity) not in {
            PosixFileIdentityV2,
            Win32FileIdentity,
        }:
            raise RegistrationSessionContractError("retained platform identity is invalid")
        expected = _digest(self._unsigned_mapping())
        if _sha(self.identity_sha256, "identity_sha256") != expected:
            raise RegistrationSessionContractError("retained file identity checksum is invalid")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "canonical_uri": self.canonical_uri,
            "platform_identity": self.platform_identity.to_mapping(),
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "identity_sha256": self.identity_sha256,
        }

    @classmethod
    def create(
        cls,
        *,
        canonical_uri: str,
        platform_identity: PosixFileIdentityV2 | Win32FileIdentity,
    ) -> "RetainedRegistrationFileIdentity":
        unsigned = {
            "canonical_uri": canonical_uri,
            "platform_identity": _wire(platform_identity),
        }
        return cls(
            canonical_uri=canonical_uri,
            platform_identity=platform_identity,
            identity_sha256=_digest(unsigned),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "RetainedRegistrationFileIdentity":
        mapping = _closed(
            value,
            frozenset({"canonical_uri", "platform_identity", "identity_sha256"}),
            "retained registration file identity",
        )
        return cls(
            canonical_uri=mapping["canonical_uri"],  # type: ignore[arg-type]
            platform_identity=_platform_identity_from_mapping(mapping["platform_identity"]),
            identity_sha256=mapping["identity_sha256"],  # type: ignore[arg-type]
        )


def _member_semantic_locator(*, entry_role: str, project_entry_id: str, target_kind: str) -> str:
    suffix = {
        "entry_qpdata": "data.qpdata",
        "entry_hierarchy_tree": "hierarchy-tree",
    }.get(target_kind)
    if suffix is None:
        raise RegistrationSessionContractError(
            "registration project member target kind is unsupported"
        )
    return f"{entry_role}/{project_entry_id}/{suffix}"


@dataclass(frozen=True, slots=True)
class RegistrationProjectMemberObservation:
    """One semantic entry member resolved by a provider-observed entry root."""

    entry_role: Literal["fixed", "moving"]
    project_entry_id: str
    target_kind: Literal["entry_qpdata", "entry_hierarchy_tree"]
    semantic_locator: str
    resolved_uri: str
    presence: Literal["present", "absent"]
    content_sha256: str | None
    byte_count: int | None
    tree_member_count: int | None
    observation_sha256: str

    def __post_init__(self) -> None:
        """Validate the RegistrationProjectMemberObservation fields when the record is
        constructed.

        The checks cover ``entry_role``, ``project_entry_id``, ``semantic_locator``,
        ``resolved_uri``, ``presence``, ``target_kind``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        if self.entry_role not in _ENTRY_ROLES:
            raise RegistrationSessionContractError("registration entry role is unsupported")
        _safe_id(self.project_entry_id, "project_entry_id")
        expected_locator = _member_semantic_locator(
            entry_role=self.entry_role,
            project_entry_id=self.project_entry_id,
            target_kind=self.target_kind,
        )
        if self.semantic_locator != expected_locator:
            raise RegistrationSessionContractError(
                "registration member semantic locator is invalid"
            )
        _local_file_uri(self.resolved_uri, "resolved_uri")
        if self.presence not in {"present", "absent"}:
            raise RegistrationSessionContractError("registration member presence is unsupported")
        if self.presence == "absent":
            if any(
                value is not None
                for value in (
                    self.content_sha256,
                    self.byte_count,
                    self.tree_member_count,
                )
            ):
                raise RegistrationSessionContractError(
                    "absent registration member cannot claim content"
                )
        else:
            _sha(self.content_sha256, "content_sha256")
            _nonnegative(self.byte_count, "byte_count")
            if self.target_kind == "entry_hierarchy_tree":
                _nonnegative(self.tree_member_count, "tree_member_count")
            elif self.tree_member_count is not None:
                raise RegistrationSessionContractError(
                    "qpdata observation cannot claim tree members"
                )
        expected = _digest(self._unsigned_mapping())
        if _sha(self.observation_sha256, "observation_sha256") != expected:
            raise RegistrationSessionContractError(
                "registration member observation checksum is invalid"
            )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name != "observation_sha256"
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "observation_sha256": self.observation_sha256,
        }

    @classmethod
    def create(cls, **values: object) -> "RegistrationProjectMemberObservation":
        _closed_factory_values(
            values,
            record_fields=cls.__dataclass_fields__,
            derived_fields=frozenset({"semantic_locator", "observation_sha256"}),
            label="registration project member",
        )
        unsigned = {
            **values,
            "semantic_locator": _member_semantic_locator(
                entry_role=values.get("entry_role"),  # type: ignore[arg-type]
                project_entry_id=values.get("project_entry_id"),  # type: ignore[arg-type]
                target_kind=values.get("target_kind"),  # type: ignore[arg-type]
            ),
        }
        ordered = {
            name: unsigned[name]
            for name in cls.__dataclass_fields__
            if name != "observation_sha256"
        }
        return cls(  # type: ignore[arg-type]
            **ordered,
            observation_sha256=_digest(ordered),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "RegistrationProjectMemberObservation":
        mapping = _closed(
            value,
            frozenset(cls.__dataclass_fields__),
            "registration project member observation",
        )
        return cls(**dict(mapping))  # type: ignore[arg-type]


def _transform_filename(fixed_id: str, moving_id: str) -> str:
    return f"transform_{fixed_id}_{moving_id}.json"


@dataclass(frozen=True, slots=True)
class WarpyTransformObservation:
    """Opaque conventional Warpy transform presence/content observation."""

    fixed_project_entry_id: str
    moving_project_entry_id: str
    semantic_locator: str
    transform_uri: str
    presence: Literal["present", "absent"]
    content_sha256: str | None
    byte_count: int | None
    observation_sha256: str

    def __post_init__(self) -> None:
        """Validate the WarpyTransformObservation fields when the record is constructed.

        The checks cover ``fixed_project_entry_id``, ``moving_project_entry_id``,
        ``semantic_locator``, ``transform_uri``, ``presence``, ``content_sha256``. Invalid
        field types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        fixed_id = _safe_id(self.fixed_project_entry_id, "fixed_project_entry_id")
        moving_id = _safe_id(self.moving_project_entry_id, "moving_project_entry_id")
        if fixed_id == moving_id:
            raise RegistrationSessionContractError(
                "Warpy fixed and moving entries must be distinct"
            )
        filename = _transform_filename(fixed_id, moving_id)
        expected_locator = f"fixed/{fixed_id}/warpy-transform/{filename}"
        if self.semantic_locator != expected_locator:
            raise RegistrationSessionContractError("Warpy transform semantic locator is invalid")
        transform_uri = _local_file_uri(self.transform_uri, "transform_uri")
        if urlsplit(transform_uri).path.rsplit("/", 1)[-1] != filename:
            raise RegistrationSessionContractError(
                "Warpy transform URI does not use the fixed/moving convention"
            )
        if self.presence not in {"present", "absent"}:
            raise RegistrationSessionContractError("Warpy transform presence is unsupported")
        if self.presence == "absent":
            if self.content_sha256 is not None or self.byte_count is not None:
                raise RegistrationSessionContractError(
                    "absent Warpy transform cannot claim content"
                )
        else:
            _sha(self.content_sha256, "content_sha256")
            _nonnegative(self.byte_count, "byte_count")
        if _sha(self.observation_sha256, "observation_sha256") != _digest(self._unsigned_mapping()):
            raise RegistrationSessionContractError(
                "Warpy transform observation checksum is invalid"
            )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name != "observation_sha256"
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "observation_sha256": self.observation_sha256,
        }

    @classmethod
    def create(cls, **values: object) -> "WarpyTransformObservation":
        _closed_factory_values(
            values,
            record_fields=cls.__dataclass_fields__,
            derived_fields=frozenset({"semantic_locator", "observation_sha256"}),
            label="Warpy transform observation",
        )
        fixed_id = _safe_id(values.get("fixed_project_entry_id"), "fixed_project_entry_id")
        moving_id = _safe_id(values.get("moving_project_entry_id"), "moving_project_entry_id")
        filename = _transform_filename(fixed_id, moving_id)
        unsigned = {
            **values,
            "semantic_locator": f"fixed/{fixed_id}/warpy-transform/{filename}",
        }
        ordered = {
            name: unsigned[name]
            for name in cls.__dataclass_fields__
            if name != "observation_sha256"
        }
        return cls(  # type: ignore[arg-type]
            **ordered,
            observation_sha256=_digest(ordered),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "WarpyTransformObservation":
        mapping = _closed(
            value,
            frozenset(cls.__dataclass_fields__),
            "Warpy transform observation",
        )
        return cls(**dict(mapping))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class RegistrationProjectEntryPyramidObservation:
    """Bounded qpproj-only scientific observation for one registration entry."""

    entry_role: Literal["fixed", "moving"]
    image_role: Literal["primary_mif", "moving_reference"]
    project_uri: str
    qpproj_sha256: str
    project_entry_id: str
    entry_projection_sha256: str
    source_uri: str
    source_image_sha256: str
    server_builder_metadata_sha256: str
    native_pyramid_inventory_sha256: str
    level0_size_x_px: int
    level0_size_y_px: int
    x_um_per_px_hex: str
    y_um_per_px_hex: str
    channel_names: tuple[str, ...]
    is_rgb: bool
    observation_sha256: str

    def __post_init__(self) -> None:
        expected = {"fixed": "primary_mif", "moving": "moving_reference"}.get(self.entry_role)
        if self.image_role != expected:
            raise RegistrationSessionContractError(
                "registration pyramid role and image role differ"
            )
        _local_file_uri(self.project_uri, "project_uri")
        _sha(self.qpproj_sha256, "qpproj_sha256")
        _safe_id(self.project_entry_id, "project_entry_id")
        _sha(self.entry_projection_sha256, "entry_projection_sha256")
        _local_file_uri(self.source_uri, "source_uri")
        for name in (
            "source_image_sha256",
            "server_builder_metadata_sha256",
            "native_pyramid_inventory_sha256",
        ):
            _sha(getattr(self, name), name)
        _positive(self.level0_size_x_px, "level0_size_x_px")
        _positive(self.level0_size_y_px, "level0_size_y_px")
        _positive_hex(self.x_um_per_px_hex, "x_um_per_px_hex")
        _positive_hex(self.y_um_per_px_hex, "y_um_per_px_hex")
        if (type(self.channel_names) is not tuple or not self.channel_names
                or any(type(name) is not str or not name for name in self.channel_names)
                or type(self.is_rgb) is not bool):
            raise RegistrationSessionContractError("registration channel metadata is invalid")
        if _sha(self.observation_sha256, "observation_sha256") != _digest(self._unsigned_mapping()):
            raise RegistrationSessionContractError(
                "registration pyramid observation checksum is invalid"
            )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            name: _wire(getattr(self, name))
            for name in self.__dataclass_fields__
            if name != "observation_sha256"
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "observation_sha256": self.observation_sha256,
        }

    @classmethod
    def create(cls, **values: object) -> "RegistrationProjectEntryPyramidObservation":
        _closed_factory_values(
            values,
            record_fields=cls.__dataclass_fields__,
            derived_fields=frozenset({"observation_sha256"}),
            label="registration project pyramid observation",
        )
        return cls(**values, observation_sha256=_digest(values))  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "RegistrationProjectEntryPyramidObservation":
        mapping = dict(_closed(
            value,
            frozenset(cls.__dataclass_fields__),
            "registration project pyramid observation",
        ))
        if not isinstance(mapping["channel_names"], list):
            raise RegistrationSessionContractError("registration channel names must be an array")
        mapping["channel_names"] = tuple(mapping["channel_names"])
        return cls(**mapping)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class RegistrationProjectEntryObservation:
    """Exact provider-neutral observation of one current QuPath entry."""

    entry_role: Literal["fixed", "moving"]
    image_role: Literal["primary_mif", "moving_reference"]
    project_entry_id: str
    entry_projection_sha256: str
    source_uri: str
    source_image_sha256: str
    source_file_identity: RetainedRegistrationFileIdentity
    entry_root_uri: str
    native_pyramid_inventory_sha256: str
    level0_size_x_px: int
    level0_size_y_px: int
    x_um_per_px_hex: str
    y_um_per_px_hex: str
    members: tuple[RegistrationProjectMemberObservation, ...]
    entry_observation_sha256: str

    def __post_init__(self) -> None:
        """Validate the RegistrationProjectEntryObservation fields when the record is
        constructed.

        The checks cover ``entry_role``, ``image_role``, ``project_entry_id``,
        ``entry_projection_sha256``, ``source_uri``, ``source_image_sha256``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        expected_image_role = {
            "fixed": "primary_mif",
            "moving": "moving_reference",
        }.get(self.entry_role)
        if self.image_role != expected_image_role:
            raise RegistrationSessionContractError("registration entry role and image role differ")
        _safe_id(self.project_entry_id, "project_entry_id")
        _sha(self.entry_projection_sha256, "entry_projection_sha256")
        source_uri = _local_file_uri(self.source_uri, "source_uri")
        _sha(self.source_image_sha256, "source_image_sha256")
        if (
            type(self.source_file_identity) is not RetainedRegistrationFileIdentity
            or self.source_file_identity.canonical_uri != source_uri
        ):
            raise RegistrationSessionContractError(
                "registration source file identity differs from entry source URI"
            )
        entry_root = _local_file_uri(self.entry_root_uri, "entry_root_uri")
        if entry_root.endswith("/") or entry_root == source_uri:
            raise RegistrationSessionContractError("registration entry root is invalid")
        _sha(self.native_pyramid_inventory_sha256, "native_pyramid_inventory_sha256")
        _positive(self.level0_size_x_px, "level0_size_x_px")
        _positive(self.level0_size_y_px, "level0_size_y_px")
        _positive_hex(self.x_um_per_px_hex, "x_um_per_px_hex")
        _positive_hex(self.y_um_per_px_hex, "y_um_per_px_hex")
        members = tuple(self.members)
        if (
            members != self.members
            or tuple(item.target_kind for item in members) != _MEMBER_KINDS
            or any(
                type(item) is not RegistrationProjectMemberObservation
                or item.entry_role != self.entry_role
                or item.project_entry_id != self.project_entry_id
                for item in members
            )
        ):
            raise RegistrationSessionContractError(
                "registration entry member inventory is not exact"
            )
        expected_uris = (
            _uri_child(entry_root, "data.qpdata"),
            entry_root,
        )
        if tuple(item.resolved_uri for item in members) != expected_uris:
            raise RegistrationSessionContractError(
                "registration entry members differ from the provider-observed root"
            )
        if _sha(self.entry_observation_sha256, "entry_observation_sha256") != _digest(
            self._unsigned_mapping()
        ):
            raise RegistrationSessionContractError(
                "registration entry observation checksum is invalid"
            )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            name: (
                self.source_file_identity.to_mapping()
                if name == "source_file_identity"
                else (
                    [item.to_mapping() for item in self.members]
                    if name == "members"
                    else getattr(self, name)
                )
            )
            for name in self.__dataclass_fields__
            if name != "entry_observation_sha256"
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "entry_observation_sha256": self.entry_observation_sha256,
        }

    @classmethod
    def create(cls, **values: object) -> "RegistrationProjectEntryObservation":
        _closed_factory_values(
            values,
            record_fields=cls.__dataclass_fields__,
            derived_fields=frozenset({"entry_observation_sha256"}),
            label="registration project entry observation",
        )
        unsigned = {
            name: (
                _wire(values[name]) if name in {"source_file_identity", "members"} else values[name]
            )
            for name in cls.__dataclass_fields__
            if name != "entry_observation_sha256"
        }
        return cls(  # type: ignore[arg-type]
            **values,
            entry_observation_sha256=_digest(unsigned),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "RegistrationProjectEntryObservation":
        mapping = dict(
            _closed(
                value,
                frozenset(cls.__dataclass_fields__),
                "registration project entry observation",
            )
        )
        mapping["source_file_identity"] = RetainedRegistrationFileIdentity.from_mapping(
            mapping["source_file_identity"]
        )
        raw_members = mapping["members"]
        if type(raw_members) is not list:
            raise RegistrationSessionContractError("registration entry members must be an array")
        mapping["members"] = tuple(
            RegistrationProjectMemberObservation.from_mapping(item) for item in raw_members
        )
        return cls(**mapping)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class CurrentRegistrationProjectSnapshot:
    """Canonical current existing-project facts for one Warpy pair."""

    schema_name: Literal["mif-registration-current-project-snapshot"]
    schema_version: Literal[1]
    project_uri: str
    project_identity_sha256: str
    project_authority_head_sha256: str
    qpproj_sha256: str
    qpproj_file_identity: RetainedRegistrationFileIdentity
    fixed_entry: RegistrationProjectEntryObservation
    moving_entry: RegistrationProjectEntryObservation
    transform: WarpyTransformObservation
    snapshot_resolution_sha256: str

    def __post_init__(self) -> None:
        """Validate the CurrentRegistrationProjectSnapshot fields when the record is
        constructed.

        The checks cover ``transform``, ``project_uri``, ``schema_name``,
        ``schema_version``, ``snapshot_resolution_sha256``, ``qpproj_file_identity``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        if (
            self.schema_name != "mif-registration-current-project-snapshot"
            or type(self.schema_version) is not int
            or self.schema_version != 1
        ):
            raise RegistrationSessionContractError(
                "current registration project snapshot schema is unsupported"
            )
        project_uri = _local_file_uri(self.project_uri, "project_uri")
        if not urlsplit(project_uri).path.endswith(".qpproj"):
            raise RegistrationSessionContractError("registration project URI must name one qpproj")
        for name in (
            "project_identity_sha256",
            "project_authority_head_sha256",
            "qpproj_sha256",
        ):
            _sha(getattr(self, name), name)
        if (
            type(self.qpproj_file_identity) is not RetainedRegistrationFileIdentity
            or self.qpproj_file_identity.canonical_uri != project_uri
        ):
            raise RegistrationSessionContractError(
                "qpproj retained identity differs from project URI"
            )
        if (
            type(self.fixed_entry) is not RegistrationProjectEntryObservation
            or type(self.moving_entry) is not RegistrationProjectEntryObservation
            or self.fixed_entry.entry_role != "fixed"
            or self.fixed_entry.image_role != "primary_mif"
            or self.moving_entry.entry_role != "moving"
            or self.moving_entry.image_role != "moving_reference"
            or self.fixed_entry.project_entry_id == self.moving_entry.project_entry_id
        ):
            raise RegistrationSessionContractError(
                "registration snapshot requires one distinct fixed/moving pair"
            )
        transform = self.transform
        if (
            type(transform) is not WarpyTransformObservation
            or transform.fixed_project_entry_id != self.fixed_entry.project_entry_id
            or transform.moving_project_entry_id != self.moving_entry.project_entry_id
            or _uri_parent(transform.transform_uri) != self.fixed_entry.entry_root_uri
        ):
            raise RegistrationSessionContractError(
                "Warpy transform differs from the fixed/moving entry scope"
            )
        if _sha(self.snapshot_resolution_sha256, "snapshot_resolution_sha256") != _digest(
            self._unsigned_mapping()
        ):
            raise RegistrationSessionContractError("registration snapshot checksum is invalid")

    @property
    def quantitative_entry(self) -> RegistrationProjectEntryObservation:
        return self.fixed_entry

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            name: (
                getattr(self, name).to_mapping()
                if name
                in {
                    "qpproj_file_identity",
                    "fixed_entry",
                    "moving_entry",
                    "transform",
                }
                else getattr(self, name)
            )
            for name in self.__dataclass_fields__
            if name != "snapshot_resolution_sha256"
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "snapshot_resolution_sha256": self.snapshot_resolution_sha256,
        }

    @classmethod
    def create(cls, **values: object) -> "CurrentRegistrationProjectSnapshot":
        forbidden = {
            "schema_name",
            "schema_version",
            "snapshot_resolution_sha256",
        }
        _closed_factory_values(
            values,
            record_fields=cls.__dataclass_fields__,
            derived_fields=frozenset(forbidden),
            label="current registration project snapshot",
        )
        unsigned = {
            "schema_name": "mif-registration-current-project-snapshot",
            "schema_version": 1,
            **{
                name: _wire(values[name])
                for name in cls.__dataclass_fields__
                if name not in forbidden
            },
        }
        return cls(  # type: ignore[arg-type]
            schema_name="mif-registration-current-project-snapshot",
            schema_version=1,
            **values,
            snapshot_resolution_sha256=_digest(unsigned),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "CurrentRegistrationProjectSnapshot":
        mapping = dict(
            _closed(
                value,
                frozenset(cls.__dataclass_fields__),
                "current registration project snapshot",
            )
        )
        mapping["qpproj_file_identity"] = RetainedRegistrationFileIdentity.from_mapping(
            mapping["qpproj_file_identity"]
        )
        mapping["fixed_entry"] = RegistrationProjectEntryObservation.from_mapping(
            mapping["fixed_entry"]
        )
        mapping["moving_entry"] = RegistrationProjectEntryObservation.from_mapping(
            mapping["moving_entry"]
        )
        mapping["transform"] = WarpyTransformObservation.from_mapping(mapping["transform"])
        return cls(**mapping)  # type: ignore[arg-type]


class CurrentRegistrationProjectSnapshotResolver:
    """Issuer-bound in-memory resolver for retained canonical snapshot bytes."""

    __slots__ = (
        "_snapshot_payload",
        "_snapshot_resolution_sha256",
        "_moving_channel_names",
        "_moving_is_rgb",
        "_issuer",
    )

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("CurrentRegistrationProjectSnapshotResolver is issuer-only")

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise TypeError("CurrentRegistrationProjectSnapshotResolver is immutable")

    def __delattr__(self, name: str) -> None:
        del name
        raise TypeError("CurrentRegistrationProjectSnapshotResolver is immutable")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("CurrentRegistrationProjectSnapshotResolver cannot be subclassed")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("CurrentRegistrationProjectSnapshotResolver is nonserializable")

    @classmethod
    def _issue(
        cls,
        *,
        snapshot: CurrentRegistrationProjectSnapshot,
        issuer: object,
        moving_channel_names: tuple[str, ...],
        moving_is_rgb: bool,
    ) -> "CurrentRegistrationProjectSnapshotResolver":
        if type(snapshot) is not CurrentRegistrationProjectSnapshot or issuer is None:
            raise RegistrationSessionContractError(
                "registration snapshot resolver issuer/input is invalid"
            )
        payload = canonical_json_bytes(snapshot.to_mapping())
        resolver = object.__new__(cls)
        object.__setattr__(resolver, "_snapshot_payload", payload)
        object.__setattr__(
            resolver,
            "_snapshot_resolution_sha256",
            snapshot.snapshot_resolution_sha256,
        )
        object.__setattr__(resolver, "_issuer", issuer)
        object.__setattr__(resolver, "_moving_channel_names", moving_channel_names)
        object.__setattr__(resolver, "_moving_is_rgb", moving_is_rgb)
        return resolver

    @property
    def moving_channel_names(self) -> tuple[str, ...]:
        return self._moving_channel_names

    @property
    def moving_is_rgb(self) -> bool:
        return self._moving_is_rgb

    @property
    def snapshot_resolution_sha256(self) -> str:
        return self._snapshot_resolution_sha256

    def resolve(self, *, issuer: object) -> CurrentRegistrationProjectSnapshot:
        if issuer is not self._issuer:
            raise RegistrationSessionContractError("registration snapshot resolver issuer differs")
        try:
            raw = json.loads(self._snapshot_payload.decode("utf-8"))
            reopened = CurrentRegistrationProjectSnapshot.from_mapping(raw)
            canonical = canonical_json_bytes(reopened.to_mapping())
        except Exception as exc:
            raise RegistrationSessionContractError(
                "retained registration snapshot bytes are invalid"
            ) from exc
        if (
            canonical != self._snapshot_payload
            or reopened.snapshot_resolution_sha256 != self._snapshot_resolution_sha256
        ):
            raise RegistrationSessionContractError("retained registration snapshot bytes changed")
        return reopened


_EXCHANGE_MEMBER_FILENAMES = {
    "registration_request_envelope": "envelope.json",
    "registration_script_request": "request.json",
    "registration_runtime_evidence": "evidence.json",
    "registration_evidence_descriptor": "descriptor.json",
    "registration_stdout_log": "stdout.log",
    "registration_stderr_log": "stderr.log",
}


@dataclass(frozen=True, slots=True)
class RegistrationRuntimeTargetPlanSeed:
    """Digest-cycle-free input to later runner-owned target planning."""

    registration_exchange_id: str
    snapshot_resolution_sha256: str
    ordered_roles: tuple[Literal["registration_import", "transfer_qc"], ...]
    seed_sha256: str

    def __post_init__(self) -> None:
        _sha(self.registration_exchange_id, "registration_exchange_id")
        _sha(self.snapshot_resolution_sha256, "snapshot_resolution_sha256")
        if type(self.ordered_roles) is not tuple or self.ordered_roles not in {
            ("registration_import",),
            ("registration_import", "transfer_qc"),
        }:
            raise RegistrationSessionContractError(
                "registration target seed roles are unsupported or unordered"
            )
        if _sha(self.seed_sha256, "seed_sha256") != _digest(self._unsigned_mapping()):
            raise RegistrationSessionContractError("registration target seed checksum is invalid")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "registration_exchange_id": self.registration_exchange_id,
            "snapshot_resolution_sha256": self.snapshot_resolution_sha256,
            "ordered_roles": list(self.ordered_roles),
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "seed_sha256": self.seed_sha256}

    @classmethod
    def create(
        cls,
        *,
        registration_exchange_id: str,
        snapshot_resolution_sha256: str,
        ordered_roles: tuple[str, ...],
    ) -> "RegistrationRuntimeTargetPlanSeed":
        unsigned = {
            "registration_exchange_id": registration_exchange_id,
            "snapshot_resolution_sha256": snapshot_resolution_sha256,
            "ordered_roles": list(ordered_roles),
        }
        return cls(
            registration_exchange_id=registration_exchange_id,
            snapshot_resolution_sha256=snapshot_resolution_sha256,
            ordered_roles=ordered_roles,  # type: ignore[arg-type]
            seed_sha256=_digest(unsigned),
        )

    def semantic_exchange_member_locator(
        self,
        *,
        role: str,
        target_kind: str,
    ) -> str:
        if role not in self.ordered_roles:
            raise RegistrationSessionContractError("registration exchange role was not planned")
        filename = _EXCHANGE_MEMBER_FILENAMES.get(target_kind)
        if filename is None:
            raise RegistrationSessionContractError(
                "registration exchange target kind is unsupported"
            )
        return f"{self.registration_exchange_id}/{role}/{filename}"


_MAX_REGISTRATION_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_REGISTRATION_JSON_DEPTH = 32
_MAX_REGISTRATION_JSON_ITEMS = 100_000
_MAX_REGISTRATION_COLLECTION = 65_536
_MAX_REGISTRATION_TEXT_BYTES = 4096
_REGISTERED_ROLE_REQUEST_FIELDS = frozenset(
    {
        "schema_name",
        "schema_version",
        "role",
        "authority_layout_sha256",
        "workspace_storage_scope_sha256",
        "active_mutation_run_sha256",
        "project_identity_sha256",
        "acquisition_record_sha256",
        "acquisition_session_challenge_sha256",
        "artifact_inventory_sha256",
        "mutation_request_sha256",
        "mutation_authorization_sha256",
        "fencing_token_sha256",
        "registration_exchange_id",
        "snapshot_resolution_sha256",
        "runtime_target_plan_sha256",
        "script_asset_id",
        "script_sha256",
        "launch_nonce_sha256",
        "script_request",
        "request_envelope_sha256",
    }
)
_WARPY_REGISTRATION_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "transform_reader",
        "registration_id",
        "project_file",
        "moving_project_entry_id",
        "fixed_project_entry_id",
        "moving_image_id",
        "fixed_image_id",
        "moving_image_path",
        "fixed_image_path",
        "moving_image_sha256",
        "fixed_image_sha256",
        "transform_file",
        "transform_file_sha256",
        "coordinate_pre_transform",
        "coordinate_post_transform",
        "accepted_tissue_sections",
        "sample_spacing_px",
    }
)
_WARPY_TRANSFER_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "transform_reader",
        "project_file",
        "registration",
        "source_object_ids",
        "accepted_tissue_sections",
        "transfer_qc_policy",
    }
)
_ACCEPTED_TISSUE_SECTION_FIELDS = frozenset(
    {
        "section_id",
        "tissue_roi_id",
        "tissue_path_object_id",
        "generation_id",
        "generation_status",
        "review_status",
        "pipeline_stage",
    }
)
_COORDINATE_TRANSFORM_FIELDS = frozenset(
    {
        "matrix",
        "authority",
        "purpose",
        "source_frame",
        "target_frame",
        "direction",
        "units",
    }
)
_REGISTRATION_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "registration_id",
        "provider",
        "provider_version",
        "moving_project_entry_id",
        "fixed_project_entry_id",
        "moving_image_id",
        "fixed_image_id",
        "moving_image_sha256",
        "fixed_image_sha256",
        "moving_image_path",
        "fixed_image_path",
        "direction",
        "moving_physical_calibration",
        "fixed_physical_calibration",
        "coordinate_pre_transform",
        "coordinate_post_transform",
        "transform_file",
        "transform_file_sha256",
        "runtime_versions",
        "moving_modality",
        "moving_image_metadata",
        "tissue_generation_id",
        "tissue_section_ids",
        "tissue_roi_ids",
        "tissue_path_object_ids",
        "revision",
        "supersedes_registration_id",
        "predecessor_record_sha256",
        "acceptance_state",
        "registration_qc",
    }
)
_REGISTRATION_QC_FIELDS = frozenset(
    {
        "schema_version",
        "qc_id",
        "registration_id",
        "tissue_generation_id",
        "sampling_context",
        "policy",
        "global_evidence",
        "section_records",
        "computed_status",
        "review_state",
        "reviewed_by",
    }
)
_REGISTRATION_SAMPLING_FIELDS = frozenset(
    {
        "target_frame",
        "sample_spacing_px",
        "grid_origin_offset_px",
        "z_position",
    }
)
_REGISTRATION_QC_POLICY_FIELDS = frozenset(
    {
        "min_coverage_fraction",
        "max_out_of_bounds_fraction",
        "min_jacobian",
        "max_jacobian",
        "require_landmarks",
        "max_landmark_rms_residual_um",
    }
)
_REGISTRATION_QC_EVIDENCE_FIELDS = frozenset(
    {
        "available",
        "unavailable_reason",
        "landmarks",
        "deformation",
        "coverage_fraction",
        "out_of_bounds_fraction",
        "bounds_finite",
    }
)
_LANDMARK_EVIDENCE_FIELDS = frozenset(
    {
        "available",
        "count",
        "rms_residual_um",
        "max_residual_um",
        "unavailable_reason",
    }
)
_DEFORMATION_EVIDENCE_FIELDS = frozenset(
    {
        "available",
        "sample_count",
        "jacobian_min",
        "jacobian_median",
        "jacobian_max",
        "negative_jacobian_count",
        "unavailable_reason",
    }
)
_SECTION_REGISTRATION_QC_FIELDS = frozenset(
    {
        "section_id",
        "tissue_roi_id",
        "tissue_path_object_id",
        "pipeline_stage",
        "generation_status",
        "review_status",
        "path_class",
        "evidence",
        "computed_status",
    }
)
_TRANSFER_QC_POLICY_FIELDS = frozenset(
    {
        "min_target_tissue_intersection_fraction",
        "min_area_ratio",
        "max_area_ratio",
        "max_fragment_count_increase",
        "max_out_of_bounds_fraction",
    }
)
_ROLE_SCRIPT_ASSETS = {
    "registration_import": ("qupath/scripts/registration/10_import_warpy_registration.groovy"),
    "transfer_qc": ("qupath/scripts/registration/11_transfer_warpy_objects.groovy"),
}


def _request_closed(value: object, fields: frozenset[str], label: str) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or any(type(key) is not str for key in value)
        or frozenset(value) != fields
    ):
        raise RegistrationSessionContractError(f"{label} fields are not the current closed schema")
    return value


def _request_array(value: object, label: str, *, nonempty: bool = False) -> list[object]:
    if type(value) is not list or len(value) > _MAX_REGISTRATION_COLLECTION:
        raise RegistrationSessionContractError(f"{label} must be one bounded JSON array")
    if nonempty and not value:
        raise RegistrationSessionContractError(f"{label} must be nonempty")
    return value


def _request_text(value: object, label: str) -> str:
    try:
        byte_count = len(value.encode("utf-8", errors="strict")) if type(value) is str else 0
    except UnicodeError as exc:
        raise RegistrationSessionContractError(f"{label} is invalid text") from exc
    if (
        type(value) is not str
        or not value.strip()
        or "\0" in value
        or byte_count > _MAX_REGISTRATION_TEXT_BYTES
    ):
        raise RegistrationSessionContractError(f"{label} is invalid text")
    return value


def _request_literal(value: object, expected: object, label: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise RegistrationSessionContractError(f"{label} must be {expected!r}")


def _request_finite(value: object, label: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float):
        raise RegistrationSessionContractError(f"{label} must be finite")
    try:
        result = float(value)
    except OverflowError as exc:
        raise RegistrationSessionContractError(f"{label} must be finite") from exc
    if not math.isfinite(result) or (positive and result <= 0.0):
        raise RegistrationSessionContractError(f"{label} must be finite")
    return result


def _request_integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**31 - 1:
        raise RegistrationSessionContractError(f"{label} is invalid")
    return value


def _request_optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _request_text(value, label)


def _request_optional_sha(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _sha(value, label)


def _export_request_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _export_request_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export_request_json(item) for item in value]
    if type(value) is list:
        return [_export_request_json(item) for item in value]
    return value


def _freeze_request_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_request_json(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze_request_json(item) for item in value)
    return value


def _validate_request_json_shape(value: object, *, depth: int = 0) -> int:
    if depth > _MAX_REGISTRATION_JSON_DEPTH:
        raise RegistrationSessionContractError("registration request JSON exceeds its depth bound")
    if isinstance(value, Mapping):
        count = len(value)
        for key, item in value.items():
            if type(key) is not str:
                raise RegistrationSessionContractError(
                    "registration request JSON keys must be strings"
                )
            count += _validate_request_json_shape(item, depth=depth + 1)
    elif type(value) is list:
        count = len(value)
        for item in value:
            count += _validate_request_json_shape(item, depth=depth + 1)
    elif value is None or type(value) in (str, bool, int):
        count = 1
    elif type(value) is float and math.isfinite(value):
        count = 1
    else:
        raise RegistrationSessionContractError(
            "registration request JSON contains an unsupported value"
        )
    if count > _MAX_REGISTRATION_JSON_ITEMS:
        raise RegistrationSessionContractError("registration request JSON exceeds its item bound")
    return count


def _reject_request_duplicate_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RegistrationSessionContractError(
                "registration request JSON contains a duplicate key"
            )
        result[key] = value
    return result


def _reject_request_nonfinite(token: str) -> object:
    raise RegistrationSessionContractError(
        f"registration request JSON contains nonfinite number {token}"
    )


def _decode_canonical_request_json(payload: bytes, label: str) -> Mapping[str, object]:
    if type(payload) is not bytes or not payload or len(payload) > _MAX_REGISTRATION_REQUEST_BYTES:
        raise RegistrationSessionContractError(f"{label} exceeds its bounded byte contract")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_request_duplicate_pairs,
            parse_constant=_reject_request_nonfinite,
        )
        _validate_request_json_shape(value)
        canonical = canonical_json_bytes(value)
    except RegistrationSessionContractError:
        raise
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
        DurableRecordError,
    ) as exc:
        raise RegistrationSessionContractError(f"{label} is invalid JSON") from exc
    if not isinstance(value, Mapping) or canonical != payload:
        raise RegistrationSessionContractError(f"{label} is not canonical JSON")
    return value


def _validate_coordinate_transform(value: object, label: str) -> None:
    if value is None:
        return
    transform = _request_closed(value, _COORDINATE_TRANSFORM_FIELDS, label)
    matrix = _request_array(transform["matrix"], f"{label} matrix")
    if len(matrix) != 3:
        raise RegistrationSessionContractError(f"{label} matrix must be three by three")
    for row in matrix:
        values = _request_array(row, f"{label} matrix row")
        if len(values) != 3:
            raise RegistrationSessionContractError(f"{label} matrix must be three by three")
        for number in values:
            _request_finite(number, f"{label} matrix value")
    for name in (
        "authority",
        "purpose",
        "source_frame",
        "target_frame",
        "direction",
        "units",
    ):
        _request_text(transform[name], f"{label} {name}")


def _validate_accepted_tissue_sections(
    value: object, label: str
) -> tuple[Mapping[str, object], ...]:
    sections: list[Mapping[str, object]] = []
    for ordinal, raw in enumerate(_request_array(value, label, nonempty=True)):
        section = _request_closed(
            raw,
            _ACCEPTED_TISSUE_SECTION_FIELDS,
            f"{label}[{ordinal}]",
        )
        for name in (
            "section_id",
            "tissue_roi_id",
            "tissue_path_object_id",
            "generation_id",
        ):
            _request_text(section[name], f"{label}[{ordinal}] {name}")
        _request_literal(
            section["generation_status"],
            "accepted",
            f"{label}[{ordinal}] generation status",
        )
        _request_literal(
            section["review_status"],
            "accepted",
            f"{label}[{ordinal}] review status",
        )
        _request_literal(
            section["pipeline_stage"],
            "tissue_detection",
            f"{label}[{ordinal}] pipeline stage",
        )
        sections.append(section)
    for name in ("section_id", "tissue_roi_id", "tissue_path_object_id"):
        identities = [section[name] for section in sections]
        if len(set(identities)) != len(identities):
            raise RegistrationSessionContractError(f"{label} {name} values must be unique")
    return tuple(sections)


def _validate_warpy_registration_script_mapping(
    value: object,
) -> Mapping[str, object]:
    request = _request_closed(
        value,
        _WARPY_REGISTRATION_REQUEST_FIELDS,
        "Warpy registration script request",
    )
    validate_transform_reader_request(request["transform_reader"])
    _request_literal(
        request["schema_version"],
        "qupath-warpy-registration-request/v2",
        "Warpy registration request schema",
    )
    for name in (
        "registration_id",
        "project_file",
        "moving_project_entry_id",
        "fixed_project_entry_id",
        "moving_image_id",
        "fixed_image_id",
        "moving_image_path",
        "fixed_image_path",
        "transform_file",
    ):
        _request_text(request[name], f"Warpy registration {name}")
    for name in (
        "moving_image_sha256",
        "fixed_image_sha256",
        "transform_file_sha256",
    ):
        _sha(request[name], f"Warpy registration {name}")
    if (
        request["moving_project_entry_id"] == request["fixed_project_entry_id"]
        or request["moving_image_id"] == request["fixed_image_id"]
        or request["moving_image_path"] == request["fixed_image_path"]
        or request["moving_image_sha256"] == request["fixed_image_sha256"]
    ):
        raise RegistrationSessionContractError(
            "Warpy registration fixed and moving identities must differ"
        )
    expected_transform_name = (
        f"transform_{request['fixed_project_entry_id']}_"
        f"{request['moving_project_entry_id']}.json"
    )
    transform_name = str(request["transform_file"]).replace("\\", "/").rsplit("/", 1)[-1]
    if transform_name != expected_transform_name:
        raise RegistrationSessionContractError(
            "Warpy registration transform path differs from its entry pair"
        )
    if (
        request["coordinate_pre_transform"] is not None
        or request["coordinate_post_transform"] is not None
    ):
        raise RegistrationSessionContractError(
            "Warpy registration coordinate pre/post transforms must be null"
        )
    _validate_accepted_tissue_sections(
        request["accepted_tissue_sections"],
        "Warpy registration accepted tissue sections",
    )
    _request_finite(
        request["sample_spacing_px"],
        "Warpy registration sample spacing",
        positive=True,
    )
    return request


def _validate_physical_calibration(value: object, label: str) -> None:
    calibration = _request_closed(value, frozenset({"x_um_per_px", "y_um_per_px"}), label)
    _request_finite(calibration["x_um_per_px"], f"{label} x_um_per_px", positive=True)
    _request_finite(calibration["y_um_per_px"], f"{label} y_um_per_px", positive=True)


def _validate_optional_finite(value: object, label: str) -> None:
    if value is not None:
        _request_finite(value, label)


def _validate_landmark_evidence(value: object, label: str) -> None:
    evidence = _request_closed(value, _LANDMARK_EVIDENCE_FIELDS, label)
    if type(evidence["available"]) is not bool:
        raise RegistrationSessionContractError(f"{label} available is invalid")
    if evidence["count"] is not None:
        _request_integer(evidence["count"], f"{label} count", minimum=1)
    _validate_optional_finite(evidence["rms_residual_um"], f"{label} rms_residual_um")
    _validate_optional_finite(evidence["max_residual_um"], f"{label} max_residual_um")
    _request_optional_text(evidence["unavailable_reason"], f"{label} unavailable_reason")


def _validate_deformation_evidence(value: object, label: str) -> None:
    evidence = _request_closed(value, _DEFORMATION_EVIDENCE_FIELDS, label)
    if type(evidence["available"]) is not bool:
        raise RegistrationSessionContractError(f"{label} available is invalid")
    if evidence["sample_count"] is not None:
        _request_integer(evidence["sample_count"], f"{label} sample_count", minimum=1)
    for name in ("jacobian_min", "jacobian_median", "jacobian_max"):
        _validate_optional_finite(evidence[name], f"{label} {name}")
    if evidence["negative_jacobian_count"] is not None:
        _request_integer(
            evidence["negative_jacobian_count"],
            f"{label} negative_jacobian_count",
        )
    _request_optional_text(evidence["unavailable_reason"], f"{label} unavailable_reason")


def _validate_registration_qc_evidence(value: object, label: str) -> None:
    evidence = _request_closed(value, _REGISTRATION_QC_EVIDENCE_FIELDS, label)
    if type(evidence["available"]) is not bool:
        raise RegistrationSessionContractError(f"{label} available is invalid")
    _request_optional_text(evidence["unavailable_reason"], f"{label} unavailable_reason")
    _validate_landmark_evidence(evidence["landmarks"], f"{label} landmarks")
    _validate_deformation_evidence(evidence["deformation"], f"{label} deformation")
    for name in ("coverage_fraction", "out_of_bounds_fraction"):
        _validate_optional_finite(evidence[name], f"{label} {name}")
    if evidence["bounds_finite"] is not None and type(evidence["bounds_finite"]) is not bool:
        raise RegistrationSessionContractError(f"{label} bounds_finite is invalid")


def _validate_registration_qc(
    value: object,
    *,
    registration_id: str,
    tissue_generation_id: str,
) -> tuple[Mapping[str, object], ...]:
    qc = _request_closed(value, _REGISTRATION_QC_FIELDS, "registration QC")
    _request_literal(
        qc["schema_version"],
        "registration-qc/v1",
        "registration QC schema",
    )
    for name in (
        "qc_id",
        "registration_id",
        "tissue_generation_id",
        "computed_status",
        "review_state",
    ):
        _request_text(qc[name], f"registration QC {name}")
    _request_optional_text(qc["reviewed_by"], "registration QC reviewed_by")
    if (
        qc["registration_id"] != registration_id
        or qc["tissue_generation_id"] != tissue_generation_id
    ):
        raise RegistrationSessionContractError(
            "registration QC identity differs from its registration"
        )
    sampling = _request_closed(
        qc["sampling_context"],
        _REGISTRATION_SAMPLING_FIELDS,
        "registration sampling context",
    )
    _request_text(sampling["target_frame"], "registration sampling target frame")
    _request_finite(
        sampling["sample_spacing_px"],
        "registration sampling spacing",
        positive=True,
    )
    offsets = _request_array(
        sampling["grid_origin_offset_px"],
        "registration sampling grid origin",
    )
    if len(offsets) != 2:
        raise RegistrationSessionContractError("registration sampling grid origin must contain XY")
    for offset in offsets:
        _request_finite(offset, "registration sampling grid origin")
    _request_finite(sampling["z_position"], "registration sampling z position")
    policy = _request_closed(qc["policy"], _REGISTRATION_QC_POLICY_FIELDS, "registration QC policy")
    for name in (
        "min_coverage_fraction",
        "max_out_of_bounds_fraction",
        "min_jacobian",
        "max_jacobian",
    ):
        _request_finite(policy[name], f"registration QC policy {name}")
    if type(policy["require_landmarks"]) is not bool:
        raise RegistrationSessionContractError("registration QC require_landmarks is invalid")
    _validate_optional_finite(
        policy["max_landmark_rms_residual_um"],
        "registration QC max landmark residual",
    )
    _validate_registration_qc_evidence(qc["global_evidence"], "global registration QC evidence")
    records: list[Mapping[str, object]] = []
    for ordinal, raw in enumerate(
        _request_array(
            qc["section_records"],
            "registration QC section records",
            nonempty=True,
        )
    ):
        record = _request_closed(
            raw,
            _SECTION_REGISTRATION_QC_FIELDS,
            f"registration QC section record {ordinal}",
        )
        for name in (
            "section_id",
            "tissue_roi_id",
            "tissue_path_object_id",
            "pipeline_stage",
            "generation_status",
            "review_status",
            "path_class",
            "computed_status",
        ):
            _request_text(record[name], f"registration QC section record {ordinal} {name}")
        _validate_registration_qc_evidence(
            record["evidence"],
            f"registration QC section record {ordinal} evidence",
        )
        records.append(record)
    return tuple(records)


def _request_text_array(value: object, label: str) -> tuple[str, ...]:
    items = tuple(
        _request_text(item, f"{label}[{ordinal}]")
        for ordinal, item in enumerate(_request_array(value, label, nonempty=True))
    )
    if len(set(items)) != len(items):
        raise RegistrationSessionContractError(f"{label} must be unique")
    return items


def _validate_registration_record(
    value: object,
) -> tuple[
    Mapping[str, object],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    registration = _request_closed(value, _REGISTRATION_RECORD_FIELDS, "Warpy registration record")
    _request_literal(
        registration["schema_version"],
        "warpy-registration/v1",
        "Warpy registration record schema",
    )
    for name in (
        "registration_id",
        "provider",
        "provider_version",
        "moving_project_entry_id",
        "fixed_project_entry_id",
        "moving_image_id",
        "fixed_image_id",
        "moving_image_path",
        "fixed_image_path",
        "direction",
        "transform_file",
        "moving_modality",
        "tissue_generation_id",
        "acceptance_state",
    ):
        _request_text(registration[name], f"Warpy registration record {name}")
    for name, expected in (
        ("provider", "warpy"),
        ("provider_version", "0.5.0"),
        ("direction", "moving_to_fixed"),
        ("acceptance_state", "accepted"),
    ):
        _request_literal(
            registration[name],
            expected,
            f"Warpy registration record {name}",
        )
    for name in (
        "moving_image_sha256",
        "fixed_image_sha256",
        "transform_file_sha256",
    ):
        _sha(registration[name], f"Warpy registration record {name}")
    if (
        registration["moving_project_entry_id"] == registration["fixed_project_entry_id"]
        or registration["moving_image_id"] == registration["fixed_image_id"]
        or registration["moving_image_path"] == registration["fixed_image_path"]
        or registration["moving_image_sha256"] == registration["fixed_image_sha256"]
    ):
        raise RegistrationSessionContractError(
            "Warpy registration record fixed and moving identities must differ"
        )
    _validate_physical_calibration(
        registration["moving_physical_calibration"],
        "moving physical calibration",
    )
    _validate_physical_calibration(
        registration["fixed_physical_calibration"],
        "fixed physical calibration",
    )
    _validate_coordinate_transform(
        registration["coordinate_pre_transform"],
        "registration record coordinate pre-transform",
    )
    _validate_coordinate_transform(
        registration["coordinate_post_transform"],
        "registration record coordinate post-transform",
    )
    expected_transform_name = (
        f"transform_{registration['fixed_project_entry_id']}_"
        f"{registration['moving_project_entry_id']}.json"
    )
    transform_name = str(registration["transform_file"]).replace("\\", "/").rsplit("/", 1)[-1]
    if transform_name != expected_transform_name:
        raise RegistrationSessionContractError(
            "Warpy registration record transform path differs from its entry pair"
        )
    runtime_versions = registration["runtime_versions"]
    if (
        not isinstance(runtime_versions, Mapping)
        or any(type(key) is not str for key in runtime_versions)
        or type(runtime_versions.get("qupath")) is not str
        or not str(runtime_versions["qupath"]).startswith("0.7.")
        or runtime_versions.get("warpy") != "0.5.0"
    ):
        raise RegistrationSessionContractError(
            "Warpy registration runtime versions must identify QuPath 0.7.x and Warpy 0.5.0"
        )
    moving_metadata = registration["moving_image_metadata"]
    if not isinstance(moving_metadata, Mapping) or any(
        type(key) is not str for key in moving_metadata
    ):
        raise RegistrationSessionContractError(
            "Warpy registration record moving_image_metadata must be an object"
        )
    section_ids = _request_text_array(
        registration["tissue_section_ids"], "registration tissue section IDs"
    )
    roi_ids = _request_text_array(registration["tissue_roi_ids"], "registration tissue ROI IDs")
    path_ids = _request_text_array(
        registration["tissue_path_object_ids"],
        "registration tissue PathObject IDs",
    )
    if not len(section_ids) == len(roi_ids) == len(path_ids):
        raise RegistrationSessionContractError("registration tissue identity arrays differ")
    _request_integer(registration["revision"], "registration revision", minimum=1)
    _request_optional_text(
        registration["supersedes_registration_id"],
        "registration predecessor ID",
    )
    _request_optional_sha(
        registration["predecessor_record_sha256"],
        "registration predecessor digest",
    )
    qc_records = _validate_registration_qc(
        registration["registration_qc"],
        registration_id=str(registration["registration_id"]),
        tissue_generation_id=str(registration["tissue_generation_id"]),
    )
    qc_identities = tuple(
        (
            str(record["section_id"]),
            str(record["tissue_roi_id"]),
            str(record["tissue_path_object_id"]),
        )
        for record in qc_records
    )
    if qc_identities != tuple(zip(section_ids, roi_ids, path_ids, strict=True)):
        raise RegistrationSessionContractError(
            "registration QC tissue identities differ from the registration"
        )
    return registration, section_ids, roi_ids, path_ids


def _validate_transfer_qc_policy(value: object) -> None:
    policy = _request_closed(value, _TRANSFER_QC_POLICY_FIELDS, "Warpy transfer QC policy")
    for name in (
        "min_target_tissue_intersection_fraction",
        "min_area_ratio",
        "max_area_ratio",
        "max_out_of_bounds_fraction",
    ):
        _request_finite(policy[name], f"Warpy transfer QC policy {name}")
    _request_integer(
        policy["max_fragment_count_increase"],
        "Warpy transfer QC policy max fragment count increase",
    )


def _validate_warpy_transfer_qc_script_mapping(
    value: object,
) -> Mapping[str, object]:
    request = _request_closed(
        value,
        _WARPY_TRANSFER_REQUEST_FIELDS,
        "Warpy transfer-QC script request",
    )
    validate_transform_reader_request(request["transform_reader"])
    _request_literal(
        request["schema_version"],
        "qupath-warpy-transfer-request/v2",
        "Warpy transfer-QC request schema",
    )
    _request_text(request["project_file"], "Warpy transfer project file")
    registration, section_ids, roi_ids, path_ids = _validate_registration_record(
        request["registration"]
    )
    _request_text_array(request["source_object_ids"], "Warpy transfer source object IDs")
    sections = _validate_accepted_tissue_sections(
        request["accepted_tissue_sections"],
        "Warpy transfer accepted tissue sections",
    )
    section_identities = tuple(
        (
            str(section["section_id"]),
            str(section["tissue_roi_id"]),
            str(section["tissue_path_object_id"]),
        )
        for section in sections
    )
    if section_identities != tuple(zip(section_ids, roi_ids, path_ids, strict=True)) or any(
        section["generation_id"] != registration["tissue_generation_id"] for section in sections
    ):
        raise RegistrationSessionContractError(
            "Warpy transfer tissue sections differ from the registration"
        )
    _validate_transfer_qc_policy(request["transfer_qc_policy"])
    return request


def decode_warpy_registration_script_request(
    payload: bytes,
) -> dict[str, object]:
    """Decode one exact current registration-import Groovy request."""

    mapping = _decode_canonical_request_json(payload, "Warpy registration script request")
    validated = _validate_warpy_registration_script_mapping(mapping)
    return _export_request_json(validated)  # type: ignore[return-value]


def decode_warpy_transfer_qc_script_request(
    payload: bytes,
) -> dict[str, object]:
    """Decode one exact current transfer-QC Groovy request."""

    mapping = _decode_canonical_request_json(payload, "Warpy transfer-QC script request")
    validated = _validate_warpy_transfer_qc_script_mapping(mapping)
    return _export_request_json(validated)  # type: ignore[return-value]


def _validate_registered_registration_role_request(
    value: object,
) -> tuple[Mapping[str, object], bytes]:
    """Validate registered registration role request for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    request = _request_closed(
        value,
        _REGISTERED_ROLE_REQUEST_FIELDS,
        "registered registration role request",
    )
    _request_literal(
        request["schema_name"],
        "mif-registered-registration-role-request",
        "registered registration role request schema",
    )
    _request_literal(
        request["schema_version"],
        1,
        "registered registration role request schema version",
    )
    role = request["role"]
    if role not in _ROLE_SCRIPT_ASSETS or type(role) is not str:
        raise RegistrationSessionContractError(
            "registered registration request role is unsupported"
        )
    for name in (
        "authority_layout_sha256",
        "workspace_storage_scope_sha256",
        "active_mutation_run_sha256",
        "project_identity_sha256",
        "acquisition_record_sha256",
        "acquisition_session_challenge_sha256",
        "artifact_inventory_sha256",
        "mutation_request_sha256",
        "mutation_authorization_sha256",
        "fencing_token_sha256",
        "registration_exchange_id",
        "snapshot_resolution_sha256",
        "runtime_target_plan_sha256",
        "script_sha256",
        "launch_nonce_sha256",
        "request_envelope_sha256",
    ):
        _sha(request[name], name)
    _request_literal(
        request["script_asset_id"],
        _ROLE_SCRIPT_ASSETS[role],
        "registered registration request script asset",
    )
    if role == "registration_import":
        script_request = _validate_warpy_registration_script_mapping(request["script_request"])
    else:
        script_request = _validate_warpy_transfer_qc_script_mapping(request["script_request"])
    try:
        script_request_bytes = canonical_json_bytes(_export_request_json(script_request))
    except DurableRecordError as exc:  # pragma: no cover - validator invariant
        raise RegistrationSessionContractError(
            "registered registration script request cannot be encoded"
        ) from exc
    unsigned = {key: request[key] for key in request if key != "request_envelope_sha256"}
    if request["request_envelope_sha256"] != _digest(unsigned):
        raise RegistrationSessionContractError(
            "registered registration request envelope digest is invalid"
        )
    return request, script_request_bytes


class RegisteredRegistrationRoleRequest:
    """Deeply immutable canonical request for one registered Warpy role."""

    __slots__ = ("_mapping", "_script_request_bytes")

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("registered registration role requests are parser/issuer-created")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("registered registration role requests cannot be subclassed")

    @classmethod
    def _from_mapping(cls, value: object) -> "RegisteredRegistrationRoleRequest":
        mapping, script_request_bytes = _validate_registered_registration_role_request(value)
        _validate_request_json_shape(mapping)
        try:
            payload = canonical_json_bytes(_export_request_json(mapping))
        except DurableRecordError as exc:  # pragma: no cover - validator invariant
            raise RegistrationSessionContractError(
                "registered registration role request cannot be encoded"
            ) from exc
        if len(payload) > _MAX_REGISTRATION_REQUEST_BYTES:
            raise RegistrationSessionContractError(
                "registered registration role request exceeds its byte bound"
            )
        instance = object.__new__(cls)
        object.__setattr__(
            instance,
            "_mapping",
            _freeze_request_json(dict(mapping)),
        )
        object.__setattr__(instance, "_script_request_bytes", script_request_bytes)
        return instance

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("registered registration role request is immutable")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("registered registration role request is immutable")

    def __getattr__(self, name: str) -> object:
        if name in _REGISTERED_ROLE_REQUEST_FIELDS:
            return self._mapping[name]
        raise AttributeError(name)

    def __eq__(self, other: object) -> bool:
        return (
            type(other) is RegisteredRegistrationRoleRequest
            and self.to_mapping() == other.to_mapping()
        )

    def __copy__(self) -> object:
        raise TypeError("registered registration role request is not copyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("registered registration role request is not copyable")

    def __reduce__(self) -> object:
        raise TypeError("registered registration role request is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("registered registration role request is nonserializable")

    @property
    def role(self) -> Literal["registration_import", "transfer_qc"]:
        return self._mapping["role"]  # type: ignore[return-value]

    @property
    def script_request(self) -> dict[str, object]:
        return _export_request_json(self._mapping["script_request"])  # type: ignore[return-value]

    @property
    def script_request_bytes(self) -> bytes:
        return self._script_request_bytes

    def to_mapping(self) -> dict[str, object]:
        return _export_request_json(self._mapping)  # type: ignore[return-value]

    def to_bytes(self) -> bytes:
        try:
            payload = canonical_json_bytes(self.to_mapping())
        except DurableRecordError as exc:  # pragma: no cover - invariant
            raise RegistrationSessionContractError(
                "registered registration role request cannot be encoded"
            ) from exc
        if len(payload) > _MAX_REGISTRATION_REQUEST_BYTES:
            raise RegistrationSessionContractError(
                "registered registration role request exceeds its byte bound"
            )
        return payload


def decode_registered_registration_role_request(
    payload: bytes,
) -> RegisteredRegistrationRoleRequest:
    """Decode one exact canonical registered Warpy role request."""

    return RegisteredRegistrationRoleRequest._from_mapping(
        _decode_canonical_request_json(payload, "registered registration role request")
    )


@dataclass(frozen=True, slots=True)
class RegistrationRoleExecutionDescriptor:
    """Canonical post-drain journal for one registered registration role."""

    schema_name: Literal["mif-registered-registration-role-descriptor"]
    schema_version: Literal[1]
    role: Literal["registration_import", "transfer_qc"]
    disposition: Literal["completed", "drained_failure"]
    failure_kind: (
        Literal[
            "nonzero_exit",
            "timeout",
            "cancelled",
            "missing_evidence",
            "invalid_evidence",
        ]
        | None
    )
    request_envelope_sha256: str
    script_request_sha256: str
    script_sha256: str
    script_bundle_sha256: str
    artifact_inventory_sha256: str
    stdout_sha256: str
    stderr_sha256: str
    stdout_truncated: bool
    stderr_truncated: bool
    spawn_attempt_sha256: str
    process_launch_sha256: str
    process_drain_sha256: str
    observed_evidence_sha256: str | None
    descriptor_sha256: str

    def __post_init__(self) -> None:
        """Validate the RegistrationRoleExecutionDescriptor fields when the record is
        constructed.

        The checks cover ``observed_evidence_sha256``, ``schema_name``, ``schema_version``,
        ``role``, ``disposition``, ``descriptor_sha256``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        if (
            self.schema_name != "mif-registered-registration-role-descriptor"
            or self.schema_version != 1
            or self.role not in {"registration_import", "transfer_qc"}
            or self.disposition not in {"completed", "drained_failure"}
            or type(self.stdout_truncated) is not bool
            or type(self.stderr_truncated) is not bool
        ):
            raise RegistrationSessionContractError(
                "registration role execution descriptor is invalid"
            )
        for name in (
            "request_envelope_sha256",
            "script_request_sha256",
            "script_sha256",
            "script_bundle_sha256",
            "artifact_inventory_sha256",
            "stdout_sha256",
            "stderr_sha256",
            "spawn_attempt_sha256",
            "process_launch_sha256",
            "process_drain_sha256",
        ):
            _sha(getattr(self, name), name)
        if self.observed_evidence_sha256 is not None:
            _sha(
                self.observed_evidence_sha256,
                "observed_evidence_sha256",
            )
        allowed_failures = {
            "nonzero_exit",
            "timeout",
            "cancelled",
            "missing_evidence",
            "invalid_evidence",
        }
        if (
            (self.disposition == "completed" and self.failure_kind is not None)
            or (self.disposition == "drained_failure" and self.failure_kind not in allowed_failures)
            or (self.disposition == "completed" and self.observed_evidence_sha256 is None)
            or (
                self.failure_kind == "missing_evidence"
                and self.observed_evidence_sha256 is not None
            )
            or (self.failure_kind == "invalid_evidence" and self.observed_evidence_sha256 is None)
        ):
            raise RegistrationSessionContractError(
                "registration role descriptor disposition is invalid"
            )
        if _sha(self.descriptor_sha256, "descriptor_sha256") != _digest(self._unsigned_mapping()):
            raise RegistrationSessionContractError(
                "registration role descriptor checksum is invalid"
            )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name != "descriptor_sha256"
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "descriptor_sha256": self.descriptor_sha256,
        }

    def to_bytes(self) -> bytes:
        try:
            return canonical_json_bytes(self.to_mapping())
        except DurableRecordError as exc:  # pragma: no cover - invariant
            raise RegistrationSessionContractError(
                "registration role descriptor cannot be encoded"
            ) from exc

    @classmethod
    def create(cls, **values: object) -> "RegistrationRoleExecutionDescriptor":
        unsigned = {
            "schema_name": "mif-registered-registration-role-descriptor",
            "schema_version": 1,
            **values,
        }
        return cls(
            **unsigned,
            descriptor_sha256=_digest(unsigned),
        )  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "RegistrationRoleExecutionDescriptor":
        return cls(
            **dict(
                _closed(
                    value,
                    frozenset(cls.__dataclass_fields__),
                    "registration role execution descriptor",
                )
            )
        )  # type: ignore[arg-type]

    @classmethod
    def from_bytes(cls, payload: bytes) -> "RegistrationRoleExecutionDescriptor":
        if type(payload) is not bytes:
            raise RegistrationSessionContractError(
                "registration role descriptor payload must be bytes"
            )
        try:
            value = json.loads(payload.decode("utf-8"))
            descriptor = cls.from_mapping(value)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistrationSessionContractError(
                "registration role descriptor is invalid JSON"
            ) from exc
        if descriptor.to_bytes() != payload:
            raise RegistrationSessionContractError("registration role descriptor is not canonical")
        return descriptor


def _issue_registered_registration_role_request(
    *,
    role: Literal["registration_import", "transfer_qc"],
    authority_layout_sha256: str,
    workspace_storage_scope_sha256: str,
    active_mutation_run_sha256: str,
    project_identity_sha256: str,
    acquisition_record_sha256: str,
    acquisition_session_challenge_sha256: str,
    artifact_inventory_sha256: str,
    mutation_request_sha256: str,
    mutation_authorization_sha256: str,
    fencing_token_sha256: str,
    registration_exchange_id: str,
    snapshot_resolution_sha256: str,
    runtime_target_plan_sha256: str,
    script_asset_id: str,
    script_sha256: str,
    launch_nonce_sha256: str,
    script_request: Mapping[str, object],
) -> RegisteredRegistrationRoleRequest:
    """Issue one canonical envelope from already-authenticated owner facts."""

    unsigned = {
        "schema_name": "mif-registered-registration-role-request",
        "schema_version": 1,
        "role": role,
        "authority_layout_sha256": authority_layout_sha256,
        "workspace_storage_scope_sha256": workspace_storage_scope_sha256,
        "active_mutation_run_sha256": active_mutation_run_sha256,
        "project_identity_sha256": project_identity_sha256,
        "acquisition_record_sha256": acquisition_record_sha256,
        "acquisition_session_challenge_sha256": (acquisition_session_challenge_sha256),
        "artifact_inventory_sha256": artifact_inventory_sha256,
        "mutation_request_sha256": mutation_request_sha256,
        "mutation_authorization_sha256": mutation_authorization_sha256,
        "fencing_token_sha256": fencing_token_sha256,
        "registration_exchange_id": registration_exchange_id,
        "snapshot_resolution_sha256": snapshot_resolution_sha256,
        "runtime_target_plan_sha256": runtime_target_plan_sha256,
        "script_asset_id": script_asset_id,
        "script_sha256": script_sha256,
        "launch_nonce_sha256": launch_nonce_sha256,
        "script_request": _export_request_json(script_request),
    }
    return RegisteredRegistrationRoleRequest._from_mapping(
        {
            **unsigned,
            "request_envelope_sha256": _digest(unsigned),
        }
    )


class ExistingRegistrationProjectSession:
    """Opaque provider-issued capability for one exact live registration lease."""

    __slots__ = ("_provider", "_issuer_identity")

    def __new__(cls, *args: object, **kwargs: object):
        del cls, args, kwargs
        raise TypeError("existing registration project sessions are provider-issued")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("existing registration project sessions cannot be subclassed")

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("existing registration project session is immutable")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("existing registration project session is immutable")

    def __copy__(self) -> object:
        raise TypeError("existing registration project session is nonserializable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("existing registration project session is nonserializable")

    def __reduce__(self) -> object:
        raise TypeError("existing registration project session is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("existing registration project session is nonserializable")

    def current_project_projection(self) -> CurrentRegistrationProjectSnapshot:
        return self._provider._existing_registration_current_project_projection(self)

    def issue_registration_import_request(
        self,
        *,
        registration_inputs: Mapping[str, object],
    ) -> RegisteredRegistrationRoleRequest:
        return self._provider._issue_existing_registration_role_request(
            self,
            role="registration_import",
            semantic_inputs=registration_inputs,
        )

    def issue_transfer_qc_request(
        self,
        *,
        transfer_inputs: Mapping[str, object],
    ) -> RegisteredRegistrationRoleRequest:
        return self._provider._issue_existing_registration_role_request(
            self,
            role="transfer_qc",
            semantic_inputs=transfer_inputs,
        )


def _create_existing_registration_project_session(
    *,
    provider: "ProjectMutationLeaseProvider",
    issuer_identity: object,
) -> ExistingRegistrationProjectSession:
    if provider is None or issuer_identity is None:
        raise RegistrationSessionContractError(
            "existing registration project session issuer is invalid"
        )
    session = object.__new__(ExistingRegistrationProjectSession)
    object.__setattr__(session, "_provider", provider)
    object.__setattr__(session, "_issuer_identity", issuer_identity)
    return session
