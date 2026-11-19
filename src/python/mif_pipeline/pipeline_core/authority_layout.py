"""Closed, platform-neutral records for the flat application authority v2.

This module defines names and authenticated references only. Provisioning, retained handles,
and mutation are deliberately owned by later composition layers.

The `ApplicationAuthorityLayoutV2` record carries `schema_name`, `schema_version`,
`platform`, `filesystem_contract`, `root_identity` and its remaining declared fields.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, Mapping, TypeAlias

from .durable_records import DurableRecordError, canonical_json_bytes
from .win32_file_authority import Win32FileIdentity

# ==============================================================================
#                                Authority layout
# ==============================================================================


class AuthorityLayoutError(DurableRecordError):
    """An authority-layout record is open, malformed, or self-inconsistent."""


AuthorityBucketNameV2: TypeAlias = Literal["immutable", "mutable", "locks", "logs", "exchange"]
AuthorityFamilyNameV2: TypeAlias = Literal[
    "project_identity",
    "project_locator",
    "project_head_history",
    "project_head_current",
    "project_lease_record",
    "project_lease_current",
    "project_lease_archive",
    "project_artifact_resolution",
    "project_mutation_attestation",
    "project_materialization_attestation",
    "project_process_attempt",
    "project_process_launch_receipt",
    "project_process_launch_current",
    "project_process_drain",
    "project_artifact_classification",
    "project_abort_finalization",
    "project_lock",
    "batch_project_generation",
    "batch_project_current",
    "batch_project_request",
    "batch_project_inventory",
    "batch_project_acquisition",
    "batch_project_runtime_evidence",
    "batch_project_replay_plan",
    "batch_project_checkpoint",
    "batch_project_runtime_replay",
    "runtime_log",
    "runtime_exchange",
    "runtime_asset",
    "member_retirement",
]
AuthoritySuffixV2: TypeAlias = Literal["json", "lock", "log", "groovy"]
AuthorityMediaTypeV2: TypeAlias = Literal[
    "application/json",
    "application/x-mif-lock",
    "application/octet-stream",
    "text/x-groovy",
]

_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_OWNER_SID = re.compile(r"S-[0-9]+(?:-[0-9]+)+\Z")
_BUCKETS: tuple[AuthorityBucketNameV2, ...] = (
    "immutable",
    "mutable",
    "locks",
    "logs",
    "exchange",
)
_MAX_LAYOUT_BYTES = 64 * 1024
_MAX_MEMBER_REF_BYTES = 4 * 1024
_MAX_RETIREMENT_BYTES = 16 * 1024
_MAX_JSON_DEPTH = 24
_MAX_PHYSICAL_NAME_BYTES = 255
_MAX_OWNER_SID_BYTES = 184
AUTHORITY_LAYOUT_MAX_BYTES_V2 = _MAX_LAYOUT_BYTES
AUTHORITY_LAYOUT_MARKER_NAME_V2 = "mif-application-authority-layout.v2.json"


def _closed_mapping(value: object, fields: frozenset[str], label: str) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or any(type(key) is not str for key in value)
        or set(value) != fields
    ):
        raise AuthorityLayoutError(f"{label} fields are invalid")
    return value


def _literal(value: object, expected: object, label: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise AuthorityLayoutError(f"{label} must be {expected!r}")


def _integer(value: object, label: str, *, maximum: int = (1 << 63) - 1) -> int:
    if type(value) is not int or value < 0 or value > maximum:
        raise AuthorityLayoutError(f"{label} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _HEX_64.fullmatch(value) is None:
        raise AuthorityLayoutError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _bounded_text(value: object, label: str) -> str:
    try:
        encoded_length = len(value.encode("utf-8", errors="strict")) if type(value) is str else 0
    except UnicodeError as exc:
        raise AuthorityLayoutError(f"{label} is not bounded canonical key text") from exc
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or encoded_length > 256
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or ":" in value
        or "\0" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise AuthorityLayoutError(f"{label} is not bounded canonical key text")
    return value


def _json_depth(value: object, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise AuthorityLayoutError("authority record exceeds its maximum JSON depth")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                raise AuthorityLayoutError("authority record keys must be strings")
            _json_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_depth(item, depth + 1)


def _canonical_authority_bytes(value: object, label: str) -> bytes:
    try:
        return canonical_json_bytes(value)
    except DurableRecordError as exc:
        raise AuthorityLayoutError(f"{label} is not canonical JSON") from exc


def _decode_canonical(payload: bytes, *, maximum: int, label: str) -> Mapping[str, object]:
    if type(payload) is not bytes or len(payload) > maximum:
        raise AuthorityLayoutError(f"{label} exceeds its bounded byte contract")
    try:
        value = json.loads(payload.decode("utf-8"))
        _json_depth(value)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AuthorityLayoutError(f"{label} is invalid JSON") from exc
    if not isinstance(value, Mapping) or _canonical_authority_bytes(value, label) != payload:
        raise AuthorityLayoutError(f"{label} is not canonical JSON")
    return value


@dataclass(frozen=True, slots=True)
class PosixFileIdentityV2:
    device: int
    inode: int

    def __post_init__(self) -> None:
        _integer(self.device, "POSIX device")
        _integer(self.inode, "POSIX inode")

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": "posix-file-identity/v2",
            "device": self.device,
            "inode": self.inode,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "PosixFileIdentityV2":
        mapping = _closed_mapping(
            value,
            frozenset({"kind", "device", "inode"}),
            "POSIX file identity",
        )
        _literal(mapping["kind"], "posix-file-identity/v2", "POSIX identity kind")
        return cls(
            device=_integer(mapping["device"], "POSIX device"),
            inode=_integer(mapping["inode"], "POSIX inode"),
        )


AuthorityDirectoryIdentityV2: TypeAlias = PosixFileIdentityV2 | Win32FileIdentity


def _identity_mapping(identity: AuthorityDirectoryIdentityV2) -> dict[str, object]:
    if isinstance(identity, PosixFileIdentityV2):
        return identity.to_mapping()
    if isinstance(identity, Win32FileIdentity):
        return identity.to_mapping()
    raise AuthorityLayoutError("authority directory identity is invalid")


def _identity_from_mapping(value: object) -> AuthorityDirectoryIdentityV2:
    if not isinstance(value, Mapping):
        raise AuthorityLayoutError("authority directory identity is invalid")
    kind = value.get("kind")
    if kind == "posix-file-identity/v2":
        return PosixFileIdentityV2.from_mapping(value)
    if kind == "win32-file-identity/v1":
        try:
            identity = Win32FileIdentity.from_mapping(value)
        except Exception as exc:
            raise AuthorityLayoutError("Win32 directory identity is invalid") from exc
        if identity.volume_serial_number > (1 << 64) - 1:
            raise AuthorityLayoutError("Win32 volume serial is invalid")
        return identity
    raise AuthorityLayoutError("authority directory identity kind is invalid")


@dataclass(frozen=True, slots=True)
class AuthorityBucketV2:
    name: AuthorityBucketNameV2
    directory_identity: AuthorityDirectoryIdentityV2

    def __post_init__(self) -> None:
        if type(self.name) is not str or self.name not in _BUCKETS:
            raise AuthorityLayoutError("authority bucket name is invalid")
        _identity_mapping(self.directory_identity)

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "directory_identity": _identity_mapping(self.directory_identity),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "AuthorityBucketV2":
        mapping = _closed_mapping(
            value,
            frozenset({"name", "directory_identity"}),
            "authority bucket",
        )
        return cls(
            name=mapping["name"],  # type: ignore[arg-type]
            directory_identity=_identity_from_mapping(mapping["directory_identity"]),
        )


@dataclass(frozen=True, slots=True)
class AuthorityFamilyV2:
    family: AuthorityFamilyNameV2
    physical_bucket: AuthorityBucketNameV2
    allowed_suffix: AuthoritySuffixV2
    media_type: AuthorityMediaTypeV2
    max_member_bytes: int
    max_members_per_scope: int

    def __post_init__(self) -> None:
        if type(self.family) is not str:
            raise AuthorityLayoutError("authority family token is invalid")
        expected = _FAMILY_BY_NAME.get(self.family)
        if expected is None:
            raise AuthorityLayoutError("authority family token is invalid")
        if expected is not self and self != expected:
            raise AuthorityLayoutError("authority family contract is not the closed v2 entry")
        if type(self.physical_bucket) is not str or self.physical_bucket not in _BUCKETS:
            raise AuthorityLayoutError("authority family bucket is invalid")
        if self.allowed_suffix not in {"json", "lock", "log", "groovy"}:
            raise AuthorityLayoutError("authority family suffix is invalid")
        if self.media_type not in {
            "application/json",
            "application/x-mif-lock",
            "application/octet-stream",
            "text/x-groovy",
        }:
            raise AuthorityLayoutError("authority family media type is invalid")
        if type(self.max_member_bytes) is not int or self.max_member_bytes <= 0:
            raise AuthorityLayoutError("authority family byte bound is invalid")
        if type(self.max_members_per_scope) is not int or self.max_members_per_scope <= 0:
            raise AuthorityLayoutError("authority family count bound is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "family": self.family,
            "physical_bucket": self.physical_bucket,
            "allowed_suffix": self.allowed_suffix,
            "media_type": self.media_type,
            "max_member_bytes": self.max_member_bytes,
            "max_members_per_scope": self.max_members_per_scope,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "AuthorityFamilyV2":
        mapping = _closed_mapping(
            value,
            frozenset(
                {
                    "family",
                    "physical_bucket",
                    "allowed_suffix",
                    "media_type",
                    "max_member_bytes",
                    "max_members_per_scope",
                }
            ),
            "authority family",
        )
        family = mapping["family"]
        if type(family) is not str or family not in _FAMILY_BY_NAME:
            raise AuthorityLayoutError("authority family token is invalid")
        candidate = cls(
            family=family,  # type: ignore[arg-type]
            physical_bucket=mapping["physical_bucket"],  # type: ignore[arg-type]
            allowed_suffix=mapping["allowed_suffix"],  # type: ignore[arg-type]
            media_type=mapping["media_type"],  # type: ignore[arg-type]
            max_member_bytes=mapping["max_member_bytes"],  # type: ignore[arg-type]
            max_members_per_scope=mapping["max_members_per_scope"],  # type: ignore[arg-type]
        )
        if candidate != _FAMILY_BY_NAME[family]:
            raise AuthorityLayoutError("authority family contract is not the closed v2 entry")
        return candidate

    def validate_payload(self, payload: bytes) -> None:
        if type(payload) is not bytes or len(payload) > self.max_member_bytes:
            raise AuthorityLayoutError("authority member exceeds its family byte bound")
        if self.media_type == "application/json":
            _decode_canonical(
                payload,
                maximum=self.max_member_bytes,
                label=f"{self.family} member",
            )
        elif self.media_type == "text/x-groovy":
            try:
                text = payload.decode("utf-8")
            except UnicodeError as exc:
                raise AuthorityLayoutError("runtime asset is invalid UTF-8") from exc
            if "\0" in text:
                raise AuthorityLayoutError("runtime asset contains NUL text")


def _family(
    family: AuthorityFamilyNameV2,
    bucket: AuthorityBucketNameV2,
    suffix: AuthoritySuffixV2,
    media: AuthorityMediaTypeV2,
    maximum: int,
    count: int,
) -> AuthorityFamilyV2:
    # Constants are built before the lookup table exists.
    instance = object.__new__(AuthorityFamilyV2)
    object.__setattr__(instance, "family", family)
    object.__setattr__(instance, "physical_bucket", bucket)
    object.__setattr__(instance, "allowed_suffix", suffix)
    object.__setattr__(instance, "media_type", media)
    object.__setattr__(instance, "max_member_bytes", maximum)
    object.__setattr__(instance, "max_members_per_scope", count)
    return instance


_JSON_MAX = 16 * 1024 * 1024
_FAMILIES: tuple[AuthorityFamilyV2, ...] = (
    _family("project_identity", "immutable", "json", "application/json", _JSON_MAX, 1),
    _family("project_locator", "mutable", "json", "application/json", _JSON_MAX, 1),
    _family("project_head_history", "immutable", "json", "application/json", _JSON_MAX, 4096),
    _family("project_head_current", "mutable", "json", "application/json", _JSON_MAX, 1),
    _family("project_lease_record", "immutable", "json", "application/json", _JSON_MAX, 4096),
    _family("project_lease_current", "mutable", "json", "application/json", _JSON_MAX, 1),
    _family("project_lease_archive", "immutable", "json", "application/json", _JSON_MAX, 4096),
    _family(
        "project_artifact_resolution", "immutable", "json", "application/json", _JSON_MAX, 4096
    ),
    _family(
        "project_mutation_attestation", "immutable", "json", "application/json", _JSON_MAX, 4096
    ),
    _family(
        "project_materialization_attestation",
        "immutable",
        "json",
        "application/json",
        _JSON_MAX,
        4096,
    ),
    _family("project_process_attempt", "immutable", "json", "application/json", _JSON_MAX, 3),
    _family(
        "project_process_launch_receipt", "immutable", "json", "application/json", _JSON_MAX, 3
    ),
    _family("project_process_launch_current", "mutable", "json", "application/json", _JSON_MAX, 3),
    _family("project_process_drain", "immutable", "json", "application/json", _JSON_MAX, 3),
    _family(
        "project_artifact_classification", "immutable", "json", "application/json", _JSON_MAX, 4096
    ),
    _family("project_abort_finalization", "immutable", "json", "application/json", _JSON_MAX, 1),
    _family("project_lock", "locks", "lock", "application/x-mif-lock", 4096, 1),
    _family("batch_project_generation", "immutable", "json", "application/json", _JSON_MAX, 4096),
    _family("batch_project_current", "mutable", "json", "application/json", _JSON_MAX, 1),
    _family("batch_project_request", "immutable", "json", "application/json", _JSON_MAX, 4096),
    _family("batch_project_inventory", "immutable", "json", "application/json", _JSON_MAX, 4096),
    _family("batch_project_acquisition", "immutable", "json", "application/json", _JSON_MAX, 4096),
    _family(
        "batch_project_runtime_evidence", "immutable", "json", "application/json", _JSON_MAX, 4096
    ),
    _family("batch_project_replay_plan", "immutable", "json", "application/json", _JSON_MAX, 1),
    _family("batch_project_checkpoint", "immutable", "json", "application/json", _JSON_MAX, 4096),
    _family("batch_project_runtime_replay", "immutable", "json", "application/json", _JSON_MAX, 1),
    _family("runtime_log", "logs", "log", "application/octet-stream", 64 * 1024 * 1024, 2),
    _family("runtime_exchange", "exchange", "json", "application/json", _JSON_MAX, 3),
    _family("runtime_asset", "immutable", "groovy", "text/x-groovy", 1024 * 1024, 128),
    _family(
        "member_retirement", "immutable", "json", "application/json", _MAX_RETIREMENT_BYTES, 4096
    ),
)
_FAMILY_BY_NAME = {item.family: item for item in _FAMILIES}
if len(_FAMILY_BY_NAME) != 30:  # pragma: no cover - import-time invariant
    raise RuntimeError("authority family table is not closed")

AUTHORITY_BUCKET_NAMES_V2 = _BUCKETS
AUTHORITY_FAMILIES_V2 = _FAMILIES


@dataclass(frozen=True, slots=True)
class Win32AuthorityProvisioningV2:
    setup_schema_version: Literal[1]
    known_folder: Literal["FOLDERID_LocalAppData"]
    filesystem_name: Literal["NTFS"]
    owner_sid: str
    dacl_sha256: str

    def __post_init__(self) -> None:
        _literal(self.setup_schema_version, 1, "Windows setup schema version")
        _literal(self.known_folder, "FOLDERID_LocalAppData", "Windows known folder")
        _literal(self.filesystem_name, "NTFS", "Windows filesystem")
        if (
            type(self.owner_sid) is not str
            or len(self.owner_sid.encode("ascii", errors="ignore")) > _MAX_OWNER_SID_BYTES
            or _OWNER_SID.fullmatch(self.owner_sid) is None
        ):
            raise AuthorityLayoutError("Windows authority owner SID is invalid")
        _digest(self.dacl_sha256, "Windows authority DACL digest")

    def to_mapping(self) -> dict[str, object]:
        return {
            "setup_schema_version": self.setup_schema_version,
            "known_folder": self.known_folder,
            "filesystem_name": self.filesystem_name,
            "owner_sid": self.owner_sid,
            "dacl_sha256": self.dacl_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "Win32AuthorityProvisioningV2":
        mapping = _closed_mapping(
            value, frozenset(cls.__dataclass_fields__), "Windows provisioning"
        )
        return cls(**dict(mapping))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class PosixAuthorityProvisioningV2:
    setup_schema_version: Literal[1]
    owner_uid: int
    owner_gid: int
    root_mode: int
    bucket_mode: int

    def __post_init__(self) -> None:
        _literal(self.setup_schema_version, 1, "POSIX setup schema version")
        _integer(self.owner_uid, "POSIX authority owner UID")
        _integer(self.owner_gid, "POSIX authority owner GID")
        if self.root_mode != 0o700 or self.bucket_mode != 0o700:
            raise AuthorityLayoutError("POSIX authority mode contract is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "setup_schema_version": self.setup_schema_version,
            "owner_uid": self.owner_uid,
            "owner_gid": self.owner_gid,
            "root_mode": self.root_mode,
            "bucket_mode": self.bucket_mode,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "PosixAuthorityProvisioningV2":
        mapping = _closed_mapping(value, frozenset(cls.__dataclass_fields__), "POSIX provisioning")
        return cls(**dict(mapping))  # type: ignore[arg-type]


AuthorityProvisioningV2: TypeAlias = Win32AuthorityProvisioningV2 | PosixAuthorityProvisioningV2


@dataclass(frozen=True, slots=True)
class ApplicationAuthorityLayoutV2:
    schema_name: Literal["mif-application-authority-layout"]
    schema_version: Literal[2]
    platform: Literal["linux", "windows"]
    filesystem_contract: Literal["posix-fsync-v2", "windows-fixed-ntfs-v2"]
    root_identity: AuthorityDirectoryIdentityV2
    key_algorithm: Literal["sha256-canonical-json-v1"]
    provisioning: AuthorityProvisioningV2
    buckets: tuple[AuthorityBucketV2, ...]
    families: tuple[AuthorityFamilyV2, ...]
    layout_sha256: str

    def __post_init__(self) -> None:
        """Validate the ApplicationAuthorityLayoutV2 fields when the record is constructed.

        The checks cover ``schema_name``, ``schema_version``, ``key_algorithm``,
        ``platform``, ``layout_sha256``, ``families``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        _literal(self.schema_name, "mif-application-authority-layout", "layout schema")
        _literal(self.schema_version, 2, "layout schema version")
        _literal(self.key_algorithm, "sha256-canonical-json-v1", "layout key algorithm")
        if (
            type(self.buckets) is not tuple
            or any(type(item) is not AuthorityBucketV2 for item in self.buckets)
            or tuple(item.name for item in self.buckets) != _BUCKETS
        ):
            raise AuthorityLayoutError("layout must contain the exact ordered five buckets")
        if (
            type(self.families) is not tuple
            or any(type(item) is not AuthorityFamilyV2 for item in self.families)
            or self.families != _FAMILIES
        ):
            raise AuthorityLayoutError("layout must contain the exact ordered 30 families")
        identities = [_identity_mapping(self.root_identity)] + [
            _identity_mapping(item.directory_identity) for item in self.buckets
        ]
        if len({_canonical_authority_bytes(item, "layout identity") for item in identities}) != len(
            identities
        ):
            raise AuthorityLayoutError("layout root and bucket identities must be distinct")
        if self.platform == "linux":
            if (
                self.filesystem_contract != "posix-fsync-v2"
                or not isinstance(self.root_identity, PosixFileIdentityV2)
                or not isinstance(self.provisioning, PosixAuthorityProvisioningV2)
                or any(
                    not isinstance(item.directory_identity, PosixFileIdentityV2)
                    for item in self.buckets
                )
            ):
                raise AuthorityLayoutError("Linux layout identity/provisioning contract is invalid")
        elif self.platform == "windows":
            if (
                self.filesystem_contract != "windows-fixed-ntfs-v2"
                or not isinstance(self.root_identity, Win32FileIdentity)
                or not isinstance(self.provisioning, Win32AuthorityProvisioningV2)
                or any(
                    not isinstance(item.directory_identity, Win32FileIdentity)
                    for item in self.buckets
                )
            ):
                raise AuthorityLayoutError(
                    "Windows layout identity/provisioning contract is invalid"
                )
        else:
            raise AuthorityLayoutError("authority layout platform is invalid")
        _digest(self.layout_sha256, "authority layout digest")
        if self.layout_sha256 != self._computed_digest():
            raise AuthorityLayoutError("authority layout self-digest is invalid")
        if (
            len(_canonical_authority_bytes(self.to_mapping(), "authority layout"))
            > _MAX_LAYOUT_BYTES
        ):
            raise AuthorityLayoutError("authority layout exceeds its bounded byte contract")

    @classmethod
    def create(
        cls,
        *,
        platform: Literal["linux", "windows"],
        filesystem_contract: Literal["posix-fsync-v2", "windows-fixed-ntfs-v2"],
        root_identity: AuthorityDirectoryIdentityV2,
        provisioning: AuthorityProvisioningV2,
        buckets: tuple[AuthorityBucketV2, ...],
    ) -> "ApplicationAuthorityLayoutV2":
        fields = {
            "schema_name": "mif-application-authority-layout",
            "schema_version": 2,
            "platform": platform,
            "filesystem_contract": filesystem_contract,
            "root_identity": _identity_mapping(root_identity),
            "key_algorithm": "sha256-canonical-json-v1",
            "provisioning": provisioning.to_mapping(),
            "buckets": [item.to_mapping() for item in buckets],
            "families": [item.to_mapping() for item in _FAMILIES],
        }
        return cls(
            schema_name="mif-application-authority-layout",
            schema_version=2,
            platform=platform,
            filesystem_contract=filesystem_contract,
            root_identity=root_identity,
            key_algorithm="sha256-canonical-json-v1",
            provisioning=provisioning,
            buckets=buckets,
            families=_FAMILIES,
            layout_sha256=hashlib.sha256(
                _canonical_authority_bytes(fields, "authority layout")
            ).hexdigest(),
        )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "platform": self.platform,
            "filesystem_contract": self.filesystem_contract,
            "root_identity": _identity_mapping(self.root_identity),
            "key_algorithm": self.key_algorithm,
            "provisioning": self.provisioning.to_mapping(),
            "buckets": [item.to_mapping() for item in self.buckets],
            "families": [item.to_mapping() for item in self.families],
        }

    def _computed_digest(self) -> str:
        return hashlib.sha256(
            _canonical_authority_bytes(self._unsigned_mapping(), "authority layout")
        ).hexdigest()

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "layout_sha256": self.layout_sha256}

    def to_bytes(self) -> bytes:
        payload = _canonical_authority_bytes(self.to_mapping(), "authority layout")
        if len(payload) > _MAX_LAYOUT_BYTES:
            raise AuthorityLayoutError("authority layout exceeds its bounded byte contract")
        return payload

    @classmethod
    def from_mapping(cls, value: object) -> "ApplicationAuthorityLayoutV2":
        """Decode the serialized fields into a validated ApplicationAuthorityLayoutV2 record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        fields = frozenset(
            {
                "schema_name",
                "schema_version",
                "platform",
                "filesystem_contract",
                "root_identity",
                "key_algorithm",
                "provisioning",
                "buckets",
                "families",
                "layout_sha256",
            }
        )
        mapping = _closed_mapping(value, fields, "authority layout")
        buckets_raw = mapping["buckets"]
        families_raw = mapping["families"]
        if (
            type(buckets_raw) is not list
            or len(buckets_raw) != len(_BUCKETS)
            or type(families_raw) is not list
            or len(families_raw) != len(_FAMILIES)
        ):
            raise AuthorityLayoutError("authority layout bucket/family inventories are invalid")
        platform = mapping["platform"]
        provisioning = (
            PosixAuthorityProvisioningV2.from_mapping(mapping["provisioning"])
            if platform == "linux"
            else (
                Win32AuthorityProvisioningV2.from_mapping(mapping["provisioning"])
                if platform == "windows"
                else None
            )
        )
        if provisioning is None:
            raise AuthorityLayoutError("authority layout platform is invalid")
        return cls(
            schema_name=mapping["schema_name"],  # type: ignore[arg-type]
            schema_version=mapping["schema_version"],  # type: ignore[arg-type]
            platform=platform,  # type: ignore[arg-type]
            filesystem_contract=mapping["filesystem_contract"],  # type: ignore[arg-type]
            root_identity=_identity_from_mapping(mapping["root_identity"]),
            key_algorithm=mapping["key_algorithm"],  # type: ignore[arg-type]
            provisioning=provisioning,
            buckets=tuple(AuthorityBucketV2.from_mapping(item) for item in buckets_raw),
            families=tuple(AuthorityFamilyV2.from_mapping(item) for item in families_raw),
            layout_sha256=mapping["layout_sha256"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ApplicationAuthorityLayoutV2":
        return cls.from_mapping(
            _decode_canonical(payload, maximum=_MAX_LAYOUT_BYTES, label="authority layout")
        )

    def family(self, name: str) -> AuthorityFamilyV2:
        try:
            return _FAMILY_BY_NAME[name]
        except (KeyError, TypeError) as exc:
            raise AuthorityLayoutError("authority family token is invalid") from exc


_KEY_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "project_identity": (("project_identity_sha256",), ()),
    "project_locator": (("project_locator_sha256",), ()),
    "project_head_history": (("project_identity_sha256",), ("head_sha256",)),
    "project_head_current": (("project_identity_sha256",), ()),
    "project_lease_record": (("project_locator_sha256",), ("lease_sha256",)),
    "project_lease_current": (("project_locator_sha256",), ()),
    "project_lease_archive": (
        ("project_identity_sha256",),
        ("fencing_token_sha256", "archive_sha256"),
    ),
    "project_artifact_resolution": (
        ("project_identity_sha256",),
        ("resolution_sha256", "resolution_disposition"),
    ),
    "project_mutation_attestation": (("project_identity_sha256",), ("attestation_sha256",)),
    "project_materialization_attestation": (("project_identity_sha256",), ("attestation_sha256",)),
    "project_process_attempt": (
        ("project_identity_sha256", "active_mutation_run_sha256"),
        ("role",),
    ),
    "project_process_launch_receipt": (
        ("project_identity_sha256", "active_mutation_run_sha256"),
        ("role",),
    ),
    "project_process_launch_current": (
        ("project_identity_sha256", "active_mutation_run_sha256"),
        ("role",),
    ),
    "project_process_drain": (
        ("project_identity_sha256", "active_mutation_run_sha256"),
        ("role",),
    ),
    "project_artifact_classification": (
        ("project_identity_sha256", "active_mutation_run_sha256"),
        ("target_sha256",),
    ),
    "project_abort_finalization": (
        ("project_identity_sha256", "active_mutation_run_sha256"),
        (),
    ),
    "project_lock": (("lock_kind", "lock_scope_sha256"), ()),
    "batch_project_generation": (("workspace_storage_scope_sha256",), ("catalogue_sha256",)),
    "batch_project_current": (("workspace_storage_scope_sha256",), ()),
    "batch_project_request": (("workspace_storage_scope_sha256",), ("request_sha256",)),
    "batch_project_inventory": (("workspace_storage_scope_sha256",), ("inventory_sha256",)),
    "batch_project_acquisition": (("workspace_storage_scope_sha256",), ("acquisition_sha256",)),
    "batch_project_runtime_evidence": (("workspace_storage_scope_sha256",), ("evidence_sha256",)),
    "batch_project_replay_plan": (
        ("workspace_storage_scope_sha256", "mutation_request_sha256"),
        (),
    ),
    "batch_project_checkpoint": (
        ("workspace_storage_scope_sha256", "mutation_request_sha256"),
        ("ordinal",),
    ),
    "batch_project_runtime_replay": (
        ("workspace_storage_scope_sha256", "mutation_request_sha256"),
        (),
    ),
    "runtime_log": (
        (
            "workspace_storage_scope_sha256",
            "active_mutation_run_sha256",
            "launch_nonce_sha256",
        ),
        ("stream",),
    ),
    "runtime_exchange": (
        (
            "workspace_storage_scope_sha256",
            "active_mutation_run_sha256",
            "role",
            "request_identity_sha256",
        ),
        ("direction",),
    ),
    "runtime_asset": (("package_version", "runtime_version"), ("asset_sha256",)),
    "member_retirement": (("retired_family", "retired_key_sha256"), ("closure_authority_sha256",)),
}
if set(_FAMILY_BY_NAME) != set(_KEY_FIELDS):  # pragma: no cover - import invariant
    raise RuntimeError("authority key table is not closed")


def _closed_key_mapping(
    family: str,
    value: Mapping[str, object],
    expected: tuple[str, ...],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise AuthorityLayoutError(f"{label} must be one string-keyed mapping")
    if tuple(sorted(value)) != tuple(sorted(expected)):
        raise AuthorityLayoutError(f"{label} fields are invalid for {family}")
    normalized: dict[str, object] = {}
    for key in expected:
        item = value[key]
        if key.endswith("_sha256"):
            normalized[key] = _digest(item, f"{label}.{key}")
        elif key == "ordinal":
            normalized[key] = _integer(item, f"{label}.ordinal", maximum=1024)
        elif key == "role":
            accepted_roles = {
                "identity_materialization",
                "primary_mif",
                "moving_reference",
            }
            if family in {
                "project_process_attempt",
                "project_process_launch_receipt",
                "project_process_launch_current",
                "project_process_drain",
                "runtime_exchange",
            }:
                accepted_roles.add("classifier_training")
            if family in {
                "project_process_attempt",
                "project_process_launch_receipt",
                "project_process_launch_current",
                "project_process_drain",
            }:
                accepted_roles.update({"registration_import", "transfer_qc"})
            if family == "project_process_drain":
                accepted_roles.add("project_stage_preparation")
            if family == "runtime_exchange":
                accepted_roles.add("project_stage")
            if type(item) is not str or item not in accepted_roles:
                raise AuthorityLayoutError(f"{label}.role is invalid")
            normalized[key] = item
        elif key == "stream":
            if type(item) is not str or item not in {"stdout", "stderr"}:
                raise AuthorityLayoutError(f"{label}.stream is invalid")
            normalized[key] = item
        elif key == "direction":
            if type(item) is not str or item not in {
                "request",
                "result",
                "generation_manifest",
            }:
                raise AuthorityLayoutError(f"{label}.direction is invalid")
            normalized[key] = item
        elif key == "resolution_disposition":
            if type(item) is not str or item not in {"planned", "final"}:
                raise AuthorityLayoutError(f"{label}.resolution_disposition is invalid")
            normalized[key] = item
        elif key == "lock_kind":
            if type(item) is not str or item not in {"locator", "identity"}:
                raise AuthorityLayoutError(f"{label}.lock_kind is invalid")
            normalized[key] = item
        elif key == "retired_family":
            if type(item) is not str or item not in _FAMILY_BY_NAME:
                raise AuthorityLayoutError(f"{label}.retired_family is invalid")
            normalized[key] = item
        else:
            normalized[key] = _bounded_text(item, f"{label}.{key}")
    return normalized


@dataclass(frozen=True, slots=True)
class AuthorityMemberRefV2:
    authority_layout_sha256: str
    family: str
    key_sha256: str
    suffix: str
    media_type: str

    def __post_init__(self) -> None:
        _digest(self.authority_layout_sha256, "member authority layout digest")
        _digest(self.key_sha256, "authority member key")
        if type(self.family) is not str:
            raise AuthorityLayoutError("authority member family is invalid")
        expected = _FAMILY_BY_NAME.get(self.family)
        if expected is None:
            raise AuthorityLayoutError("authority member family is invalid")
        if self.suffix != expected.allowed_suffix or self.media_type != expected.media_type:
            raise AuthorityLayoutError("authority member suffix/media contract is invalid")
        if len(self.physical_name.encode("utf-8")) > _MAX_PHYSICAL_NAME_BYTES:
            raise AuthorityLayoutError("authority member physical name exceeds its bound")

    @property
    def physical_name(self) -> str:
        return f"{self.family}-{self.key_sha256}.{self.suffix}"

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_name": "mif-authority-member-ref",
            "schema_version": 2,
            "authority_layout_sha256": self.authority_layout_sha256,
            "family": self.family,
            "key_sha256": self.key_sha256,
            "suffix": self.suffix,
            "media_type": self.media_type,
        }

    def to_bytes(self) -> bytes:
        payload = _canonical_authority_bytes(self.to_mapping(), "authority member reference")
        if len(payload) > _MAX_MEMBER_REF_BYTES:
            raise AuthorityLayoutError(
                "authority member reference exceeds its bounded byte contract"
            )
        return payload

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        expected_layout_sha256: str | None = None,
    ) -> "AuthorityMemberRefV2":
        mapping = _closed_mapping(
            value,
            frozenset(
                {
                    "schema_name",
                    "schema_version",
                    "authority_layout_sha256",
                    "family",
                    "key_sha256",
                    "suffix",
                    "media_type",
                }
            ),
            "authority member reference",
        )
        _literal(mapping["schema_name"], "mif-authority-member-ref", "member ref schema")
        _literal(mapping["schema_version"], 2, "member ref schema version")
        result = cls(
            authority_layout_sha256=mapping["authority_layout_sha256"],  # type: ignore[arg-type]
            family=mapping["family"],  # type: ignore[arg-type]
            key_sha256=mapping["key_sha256"],  # type: ignore[arg-type]
            suffix=mapping["suffix"],  # type: ignore[arg-type]
            media_type=mapping["media_type"],  # type: ignore[arg-type]
        )
        if expected_layout_sha256 is not None and result.authority_layout_sha256 != _digest(
            expected_layout_sha256, "expected authority layout digest"
        ):
            raise AuthorityLayoutError("authority member belongs to a different layout")
        return result

    @classmethod
    def from_bytes(
        cls,
        payload: bytes,
        *,
        expected_layout_sha256: str | None = None,
    ) -> "AuthorityMemberRefV2":
        return cls.from_mapping(
            _decode_canonical(
                payload,
                maximum=_MAX_MEMBER_REF_BYTES,
                label="authority member reference",
            ),
            expected_layout_sha256=expected_layout_sha256,
        )


def authority_member_ref_v2(
    layout: ApplicationAuthorityLayoutV2,
    *,
    family: str,
    scope: Mapping[str, object],
    natural_key: Mapping[str, object],
) -> AuthorityMemberRefV2:
    if type(layout) is not ApplicationAuthorityLayoutV2:
        raise AuthorityLayoutError("authority layout capability is invalid")
    contract = layout.family(family)
    scope_fields, key_fields = _KEY_FIELDS[family]
    closed_scope = _closed_key_mapping(family, scope, scope_fields, "scope")
    closed_key = _closed_key_mapping(family, natural_key, key_fields, "natural_key")
    key_sha256 = hashlib.sha256(
        _canonical_authority_bytes(
            {
                "authority_layout_sha256": layout.layout_sha256,
                "family": family,
                "scope": closed_scope,
                "natural_key": closed_key,
            },
            "authority member key",
        )
    ).hexdigest()
    return AuthorityMemberRefV2(
        authority_layout_sha256=layout.layout_sha256,
        family=family,
        key_sha256=key_sha256,
        suffix=contract.allowed_suffix,
        media_type=contract.media_type,
    )


@dataclass(frozen=True, slots=True)
class AuthorityMemberRetirementV2:
    schema_name: Literal["mif-authority-member-retirement"]
    schema_version: Literal[2]
    authority_layout_sha256: str
    retired_member: AuthorityMemberRefV2
    retired_payload_sha256: str
    closure_kind: Literal["successor", "checkpoint", "archive", "abort"]
    closure_authority_sha256: str
    retirement_sha256: str

    def __post_init__(self) -> None:
        _literal(self.schema_name, "mif-authority-member-retirement", "retirement schema")
        _literal(self.schema_version, 2, "retirement schema version")
        _digest(self.authority_layout_sha256, "retirement layout digest")
        _digest(self.retired_payload_sha256, "retired payload digest")
        _digest(self.closure_authority_sha256, "retirement closure digest")
        _digest(self.retirement_sha256, "retirement digest")
        if type(self.retired_member) is not AuthorityMemberRefV2:
            raise AuthorityLayoutError("retirement member reference is invalid")
        if self.retired_member.authority_layout_sha256 != self.authority_layout_sha256:
            raise AuthorityLayoutError("retirement member belongs to another layout")
        if type(self.closure_kind) is not str or self.closure_kind not in {
            "successor",
            "checkpoint",
            "archive",
            "abort",
        }:
            raise AuthorityLayoutError("retirement closure kind is invalid")
        if self.retirement_sha256 != self._computed_digest():
            raise AuthorityLayoutError("retirement self-digest is invalid")

    @classmethod
    def create(
        cls,
        *,
        layout: ApplicationAuthorityLayoutV2,
        retired_member: AuthorityMemberRefV2,
        retired_payload_sha256: str,
        closure_kind: Literal["successor", "checkpoint", "archive", "abort"],
        closure_authority_sha256: str,
    ) -> "AuthorityMemberRetirementV2":
        if type(layout) is not ApplicationAuthorityLayoutV2:
            raise AuthorityLayoutError("authority layout capability is invalid")
        if type(retired_member) is not AuthorityMemberRefV2:
            raise AuthorityLayoutError("retirement member reference is invalid")
        if retired_member.authority_layout_sha256 != layout.layout_sha256:
            raise AuthorityLayoutError("retirement member belongs to another layout")
        unsigned = {
            "schema_name": "mif-authority-member-retirement",
            "schema_version": 2,
            "authority_layout_sha256": layout.layout_sha256,
            "retired_member": retired_member.to_mapping(),
            "retired_payload_sha256": _digest(retired_payload_sha256, "retired payload digest"),
            "closure_kind": closure_kind,
            "closure_authority_sha256": _digest(
                closure_authority_sha256, "retirement closure digest"
            ),
        }
        return cls(
            schema_name="mif-authority-member-retirement",
            schema_version=2,
            authority_layout_sha256=layout.layout_sha256,
            retired_member=retired_member,
            retired_payload_sha256=unsigned["retired_payload_sha256"],  # type: ignore[arg-type]
            closure_kind=closure_kind,
            closure_authority_sha256=unsigned["closure_authority_sha256"],  # type: ignore[arg-type]
            retirement_sha256=hashlib.sha256(
                _canonical_authority_bytes(unsigned, "authority member retirement")
            ).hexdigest(),
        )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "authority_layout_sha256": self.authority_layout_sha256,
            "retired_member": self.retired_member.to_mapping(),
            "retired_payload_sha256": self.retired_payload_sha256,
            "closure_kind": self.closure_kind,
            "closure_authority_sha256": self.closure_authority_sha256,
        }

    def _computed_digest(self) -> str:
        return hashlib.sha256(
            _canonical_authority_bytes(self._unsigned_mapping(), "authority member retirement")
        ).hexdigest()

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "retirement_sha256": self.retirement_sha256}

    def to_bytes(self) -> bytes:
        payload = _canonical_authority_bytes(self.to_mapping(), "authority member retirement")
        if len(payload) > _MAX_RETIREMENT_BYTES:
            raise AuthorityLayoutError(
                "authority member retirement exceeds its bounded byte contract"
            )
        return payload

    @classmethod
    def from_mapping(cls, value: object) -> "AuthorityMemberRetirementV2":
        mapping = _closed_mapping(
            value,
            frozenset(
                {
                    "schema_name",
                    "schema_version",
                    "authority_layout_sha256",
                    "retired_member",
                    "retired_payload_sha256",
                    "closure_kind",
                    "closure_authority_sha256",
                    "retirement_sha256",
                }
            ),
            "authority member retirement",
        )
        return cls(
            schema_name=mapping["schema_name"],  # type: ignore[arg-type]
            schema_version=mapping["schema_version"],  # type: ignore[arg-type]
            authority_layout_sha256=mapping["authority_layout_sha256"],  # type: ignore[arg-type]
            retired_member=AuthorityMemberRefV2.from_mapping(
                mapping["retired_member"],
                expected_layout_sha256=mapping["authority_layout_sha256"],  # type: ignore[arg-type]
            ),
            retired_payload_sha256=mapping["retired_payload_sha256"],  # type: ignore[arg-type]
            closure_kind=mapping["closure_kind"],  # type: ignore[arg-type]
            closure_authority_sha256=mapping["closure_authority_sha256"],  # type: ignore[arg-type]
            retirement_sha256=mapping["retirement_sha256"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "AuthorityMemberRetirementV2":
        return cls.from_mapping(
            _decode_canonical(
                payload, maximum=_MAX_RETIREMENT_BYTES, label="authority member retirement"
            )
        )


__all__ = [
    "AUTHORITY_BUCKET_NAMES_V2",
    "AUTHORITY_FAMILIES_V2",
    "AUTHORITY_LAYOUT_MAX_BYTES_V2",
    "AUTHORITY_LAYOUT_MARKER_NAME_V2",
    "ApplicationAuthorityLayoutV2",
    "AuthorityBucketV2",
    "AuthorityFamilyV2",
    "AuthorityLayoutError",
    "AuthorityMemberRefV2",
    "AuthorityMemberRetirementV2",
    "PosixAuthorityProvisioningV2",
    "PosixFileIdentityV2",
    "Win32AuthorityProvisioningV2",
    "authority_member_ref_v2",
    "canonical_json_bytes",
]
