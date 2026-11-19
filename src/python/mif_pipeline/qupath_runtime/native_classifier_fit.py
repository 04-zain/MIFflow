"""Durable runtime identity for one completed native QuPath classifier fit.

The `QuPathNativeClassifierFitMaterialRecord` record carries `schema_name`,
`schema_version`, `authority_layout_sha256`, `workspace_storage_scope_sha256`,
`active_mutation_run_sha256` and its remaining declared fields.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
from threading import RLock
from typing import TYPE_CHECKING, ClassVar, Literal, NoReturn
import weakref

from mif_pipeline.pipeline_core.authority_layout import AuthorityMemberRefV2
from mif_pipeline.pipeline_core.durable_records import DurableRecordError

from .catalogue import ProjectAuthorityError

if TYPE_CHECKING:
    from .catalogue import ProjectMutationLease, ProjectMutationLeaseProvider


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_TEXT_BYTES = 4096
_MAX_CLASSIFIER_BYTES = 64 * 1024 * 1024 * 1024


# ==============================================================================
#                             Native classifier fit
# ==============================================================================


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProjectAuthorityError("native fit material is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _closed(value: object, fields: frozenset[str], label: str) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or any(type(key) is not str for key in value)
        or set(value) != fields
    ):
        raise ProjectAuthorityError(f"{label} fields are invalid")
    return dict(value)


def _sha(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ProjectAuthorityError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _text(value: object, label: str) -> str:
    try:
        encoded = value.encode("utf-8", errors="strict") if type(value) is str else b""
    except UnicodeError as exc:
        raise ProjectAuthorityError(f"{label} is invalid text") from exc
    if (
        type(value) is not str
        or not value.strip()
        or "\x00" in value
        or len(encoded) > _MAX_TEXT_BYTES
    ):
        raise ProjectAuthorityError(f"{label} is invalid text")
    return value


def _project_relative_path(value: object) -> str:
    text = _text(value, "native classifier project-relative path")
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or len(path.parts) != 3
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in text
        or path.suffix != ".json"
    ):
        raise ProjectAuthorityError("native classifier project-relative path is invalid")
    return text


def validate_training_observations(value: object) -> dict[str, object]:
    """Read selected fitting counts without interpreting them as subject counts.

    We retain separate class and image totals from the completed native fit.
    These margins describe the selected observations after any sampling limit;
    they do not describe the number of extracted rows before that limit.
    """
    row = _closed(dict(value) if isinstance(value, Mapping) else value, frozenset({"total", "classes", "images"}), "fitting observations")
    total = row["total"]
    if type(total) is not int or total < 1:
        raise ProjectAuthorityError("Fitting observations require a positive total")
    for key, fields, identity in (
        ("classes", {"class_id", "name", "count"}, "class_id"),
        ("images", {"entry_id", "sample_id", "subject_id", "count"}, "entry_id"),
    ):
        values = row[key]
        if not isinstance(values, (list, tuple)) or not values:
            raise ProjectAuthorityError("Fitting observations require class and image counts")
        identifiers = []
        converted = []
        for item in values:
            item = _closed(dict(item) if isinstance(item, Mapping) else item, frozenset(fields), "fitting observation row")
            for name in fields - {"count"}:
                _text(item[name], name)
            if type(item["count"]) is not int or item["count"] < 0:
                raise ProjectAuthorityError("Fitting observation counts must be nonnegative integers")
            identifiers.append(item[identity])
            converted.append(item)
        row[key] = converted
        if len(set(identifiers)) != len(identifiers) or sum(item["count"] for item in values) != total:
            raise ProjectAuthorityError("Fitting observation totals differ")
    return row


def native_training_observations(request, result) -> dict[str, object] | None:
    """Retain counted cells, pixels or patches from the validated fit result.

    Image and class labels come from that fit's accepted training request.
    Missing display names retain their recorded class ID; counts are never inferred.
    """
    source = request.to_mapping()
    observed = result.to_mapping()
    class_names = {}
    entries = {}
    for entry in source["training_entries"]:
        state = entry["entry_state"] if request.native_classifier_type == "pixel" else entry["snapshot"]
        entries[state["project_entry_id"]] = state
        if request.native_classifier_type == "pixel":
            for reference in entry["evidence_rows"]:
                class_names[reference["class_id"]] = reference["path_class_id"]
    return validate_training_observations({
        "total": observed["training_sample_count"],
        "classes": [
            {"class_id": key, "name": class_names.get(key, key), "count": count}
            for key, count in observed["observed_class_sample_counts"].items()
        ],
        "images": [
            {"entry_id": key, "sample_id": entries[key]["sample_id"],
             "subject_id": entries[key]["subject_id"], "count": count}
            for key, count in observed["observed_entry_sample_counts"].items()
        ],
    })


@dataclass(frozen=True, slots=True)
class QuPathNativeClassifierFitMaterialRecord:
    schema_name: Literal["mif-qupath-native-classifier-fit-material"]
    schema_version: Literal[1]
    authority_layout_sha256: str
    workspace_storage_scope_sha256: str
    active_mutation_run_sha256: str
    project_identity_sha256: str
    predecessor_project_authority_head_sha256: str
    qpproj_snapshot_sha256: str
    acquisition_record_sha256: str
    acquisition_session_challenge_sha256: str
    artifact_inventory_sha256: str
    mutation_request_sha256: str
    mutation_authorization_sha256: str
    fencing_token_sha256: str
    fit_plan_sha256: str
    fit_execution_sha256: str
    training_material_evidence_sha256: str
    classifier_id: str
    classifier_kind: str
    native_classifier_type: Literal["pixel", "object"]
    classifier_version: str
    project_relative_path: str
    native_classifier_sha256: str
    native_classifier_byte_count: int
    request_envelope_sha256: str
    result_envelope_sha256: str
    spawn_attempt_sha256: str
    process_launch_sha256: str
    process_drain_sha256: str
    script_asset_id: str
    script_sha256: str
    launch_environment_sha256: str
    containment_provider: str
    qupath_version: str
    fit_material_sha256: str
    model_selection_json: str | None = None
    training_observations_json: str | None = None

    def __post_init__(self) -> None:
        """Validate the QuPathNativeClassifierFitMaterialRecord fields when the record is
        constructed.

        The checks cover ``model_selection_json``, ``classifier_kind``,
        ``native_classifier_type``, ``schema_name``, ``schema_version``,
        ``fit_execution_sha256``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if (
            self.schema_name != "mif-qupath-native-classifier-fit-material"
            or type(self.schema_version) is not int
            or self.schema_version not in {1, 2}
        ):
            raise ProjectAuthorityError("native fit material schema is unsupported")
        if (self.schema_version == 1) != (self.model_selection_json is None):
            raise ProjectAuthorityError(
                "The native material version and model-selection report differ"
            )
        if self.model_selection_json is not None:
            if (
                type(self.model_selection_json) is not str
                or len(self.model_selection_json.encode()) > 8 * 1024 * 1024
            ):
                raise ProjectAuthorityError("Native model selection report is not bounded text")
            try:
                report = json.loads(self.model_selection_json)
                if (
                    _canonical_bytes(report).decode() != self.model_selection_json
                    or report.get("schema") != "mif-grouped-cv-selection/v1"
                ):
                    raise ValueError("noncanonical selection report")
            except (ValueError, TypeError, AttributeError) as exc:
                raise ProjectAuthorityError("Invalid retained model selection report") from exc
        if self.training_observations_json is not None:
            try:
                summary = validate_training_observations(json.loads(self.training_observations_json))
                if _canonical_bytes(summary).decode() != self.training_observations_json:
                    raise ValueError("noncanonical fitting observations")
            except (TypeError, ValueError) as exc:
                raise ProjectAuthorityError("Invalid retained fitting observations") from exc
        for name in (
            "authority_layout_sha256",
            "workspace_storage_scope_sha256",
            "active_mutation_run_sha256",
            "project_identity_sha256",
            "predecessor_project_authority_head_sha256",
            "qpproj_snapshot_sha256",
            "acquisition_record_sha256",
            "acquisition_session_challenge_sha256",
            "artifact_inventory_sha256",
            "mutation_request_sha256",
            "mutation_authorization_sha256",
            "fencing_token_sha256",
            "fit_plan_sha256",
            "fit_execution_sha256",
            "training_material_evidence_sha256",
            "native_classifier_sha256",
            "request_envelope_sha256",
            "result_envelope_sha256",
            "spawn_attempt_sha256",
            "process_launch_sha256",
            "process_drain_sha256",
            "script_sha256",
            "launch_environment_sha256",
        ):
            _sha(getattr(self, name), name)
        for name in (
            "classifier_id",
            "classifier_kind",
            "classifier_version",
            "script_asset_id",
            "containment_provider",
            "qupath_version",
        ):
            _text(getattr(self, name), name)
        if self.classifier_kind not in {"pixel", "supervised_phenotype"}:
            raise ProjectAuthorityError("native fit classifier kind is invalid")
        expected_native = "object" if self.classifier_kind == "supervised_phenotype" else "pixel"
        if self.native_classifier_type != expected_native:
            raise ProjectAuthorityError("native fit classifier type is invalid")
        expected_version = f"mif-fit-{self.fit_execution_sha256}"
        expected_path = (
            "classifiers/object_classifiers/"
            if self.native_classifier_type == "object"
            else "classifiers/pixel_classifiers/"
        ) + f"mif-{self.fit_execution_sha256}.json"
        if (
            self.classifier_version != expected_version
            or _project_relative_path(self.project_relative_path) != expected_path
            or self.script_asset_id != "qupath/scripts/general/02_train_native_classifier.groovy"
            or self.containment_provider
            not in {
                "linux-systemd-scope-broker/v1",
                "linux-local-process-group/v1",
                "windows-kill-on-close-job/v1",
            }
        ):
            raise ProjectAuthorityError("native fit material semantics differ")
        if (
            type(self.native_classifier_byte_count) is not int
            or not 1 <= self.native_classifier_byte_count <= _MAX_CLASSIFIER_BYTES
        ):
            raise ProjectAuthorityError("native fit classifier byte count is invalid")
        if _sha(self.fit_material_sha256, "fit_material_sha256") != _digest(
            self._unsigned_mapping()
        ):
            raise ProjectAuthorityError("native fit material checksum is not canonical")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name != "fit_material_sha256"
            and not (name in {"model_selection_json", "training_observations_json"} and getattr(self, name) is None)
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "fit_material_sha256": self.fit_material_sha256,
        }

    @classmethod
    def _create(cls, **values: object) -> "QuPathNativeClassifierFitMaterialRecord":
        values = dict(values)
        if values.get("model_selection_json") is None:
            values.pop("model_selection_json", None)
        if values.get("training_observations_json") is None:
            values.pop("training_observations_json", None)
        unsigned = {
            "schema_name": "mif-qupath-native-classifier-fit-material",
            "schema_version": 2 if "model_selection_json" in values else 1,
            **values,
        }
        return cls(
            **unsigned,
            fit_material_sha256=_digest(unsigned),
        )  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "QuPathNativeClassifierFitMaterialRecord":
        return cls(
            **_closed(
                value,
                frozenset(cls.__dataclass_fields__)
                - (
                    {"model_selection_json"}
                    if isinstance(value, dict) and value.get("schema_version") == 1
                    else set()
                )
                - ({"training_observations_json"} if isinstance(value, dict) and "training_observations_json" not in value else set()),
                "native fit material",
            )
        )  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class _NativeTrainingRecoveryBindingRecord:
    """Small deterministic locator for original runtime-exchange bytes."""

    schema_name: Literal["mif-qupath-native-training-recovery-binding"]
    schema_version: Literal[1]
    authority_layout_sha256: str
    workspace_storage_scope_sha256: str
    active_mutation_run_sha256: str
    project_identity_sha256: str
    acquisition_record_sha256: str
    acquisition_session_challenge_sha256: str
    artifact_inventory_sha256: str
    mutation_request_sha256: str
    mutation_authorization_sha256: str
    fencing_token_sha256: str
    launch_nonce_sha256: str
    request_envelope_sha256: str
    request_identity_sha256: str
    script_ref: AuthorityMemberRefV2
    request_ref: AuthorityMemberRefV2
    result_ref: AuthorityMemberRefV2
    stdout_ref: AuthorityMemberRefV2
    stderr_ref: AuthorityMemberRefV2
    recovery_binding_sha256: str

    def __post_init__(self) -> None:
        """Validate the _NativeTrainingRecoveryBindingRecord fields when the record is
        constructed.

        The checks cover ``script_ref``, ``request_ref``, ``result_ref``, ``stdout_ref``,
        ``stderr_ref``, ``schema_name``. Invalid field types, values or combinations raise
        the declared validation error before this instance can be used by its caller."""

        if (
            self.schema_name != "mif-qupath-native-training-recovery-binding"
            or type(self.schema_version) is not int
            or self.schema_version != 1
        ):
            raise ProjectAuthorityError("native training recovery binding schema is unsupported")
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
            "launch_nonce_sha256",
            "request_envelope_sha256",
            "request_identity_sha256",
        ):
            _sha(getattr(self, name), name)
        refs = (
            self.script_ref,
            self.request_ref,
            self.result_ref,
            self.stdout_ref,
            self.stderr_ref,
        )
        if any(
            type(ref) is not AuthorityMemberRefV2
            or ref.authority_layout_sha256 != self.authority_layout_sha256
            for ref in refs
        ):
            raise ProjectAuthorityError("native training recovery binding refs are invalid")
        if (
            self.script_ref.family != "runtime_asset"
            or self.request_ref.family != "runtime_exchange"
            or self.result_ref.family != "runtime_exchange"
            or self.stdout_ref.family != "runtime_log"
            or self.stderr_ref.family != "runtime_log"
            or self.request_ref == self.result_ref
            or self.stdout_ref == self.stderr_ref
        ):
            raise ProjectAuthorityError("native training recovery binding ref families differ")
        if _sha(self.recovery_binding_sha256, "recovery_binding_sha256") != _digest(
            self._unsigned_mapping()
        ):
            raise ProjectAuthorityError("native training recovery binding checksum differs")

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            name: (
                value.to_mapping()
                if name in {"script_ref", "request_ref", "result_ref", "stdout_ref", "stderr_ref"}
                else value
            )
            for name in self.__dataclass_fields__
            if name != "recovery_binding_sha256"
            for value in (getattr(self, name),)
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "recovery_binding_sha256": self.recovery_binding_sha256,
        }

    @classmethod
    def create(cls, **values: object) -> "_NativeTrainingRecoveryBindingRecord":
        unsigned = {
            "schema_name": "mif-qupath-native-training-recovery-binding",
            "schema_version": 1,
            **values,
        }
        prototype = dict(unsigned)
        for name in ("script_ref", "request_ref", "result_ref", "stdout_ref", "stderr_ref"):
            ref = prototype.get(name)
            if type(ref) is not AuthorityMemberRefV2:
                raise ProjectAuthorityError("native training recovery binding ref is invalid")
            prototype[name] = ref.to_mapping()
        return cls(
            **unsigned,
            recovery_binding_sha256=_digest(prototype),
        )  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, value: object) -> "_NativeTrainingRecoveryBindingRecord":
        material = _closed(
            value,
            frozenset(cls.__dataclass_fields__),
            "native training recovery binding",
        )
        try:
            for name in ("script_ref", "request_ref", "result_ref", "stdout_ref", "stderr_ref"):
                material[name] = AuthorityMemberRefV2.from_mapping(
                    material[name],
                    expected_layout_sha256=material[
                        "authority_layout_sha256"
                    ],  # type: ignore[arg-type]
                )
        except (DurableRecordError, TypeError, ValueError) as exc:
            raise ProjectAuthorityError("native training recovery binding ref is invalid") from exc
        return cls(**material)  # type: ignore[arg-type]


class _OpaqueFitValue:
    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("native fit authorities are provider-issued")

    @classmethod
    def _issue(cls, **values: object):
        value = object.__new__(cls)
        for name, item in values.items():
            object.__setattr__(value, name, item)
        return value


@dataclass(frozen=True, slots=True, init=False, repr=False)
class QuPathNativeClassifierFitCommit(_OpaqueFitValue):
    material_record: QuPathNativeClassifierFitMaterialRecord
    mutation_attestation_sha256: str
    successor_project_authority_head_sha256: str
    _lease: ProjectMutationLease
    _provider: ProjectMutationLeaseProvider
    _issuer: object


@dataclass(frozen=True, slots=True, init=False, repr=False)
class QuPathNativeClassifierFitAuthority(_OpaqueFitValue):
    material_record: QuPathNativeClassifierFitMaterialRecord
    mutation_attestation_sha256: str
    successor_project_authority_head_sha256: str
    _provider: ProjectMutationLeaseProvider
    _issuer: object


_READ_ONLY_EVIDENCE_ISSUER = object()
_READ_ONLY_EVIDENCE_LOCK = RLock()
_READ_ONLY_EVIDENCE_ISSUANCE: dict[
    int,
    tuple[
        weakref.ReferenceType["QuPathNativeClassifierReadOnlyEvidence"],
        weakref.ReferenceType[object],
        bytes,
        str,
    ],
] = {}


def _read_only_evidence_value(value: object) -> object:
    if type(value) is AuthorityMemberRefV2:
        return value.to_mapping()
    if type(value) is bytes:
        return {"bytes_hex": value.hex()}
    return value


def _read_only_evidence_seal(
    evidence: "QuPathNativeClassifierReadOnlyEvidence", nonce: bytes
) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            {
                name: _read_only_evidence_value(getattr(evidence, name))
                for name in evidence._PUBLIC_FIELDS
            }
            | {"issuer_nonce_sha256": hashlib.sha256(nonce).hexdigest()}
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True, init=False, repr=False, weakref_slot=True)
class QuPathNativeClassifierReadOnlyEvidence:
    """Opaque provider proof for one unchanged native-classifier evaluation."""

    authority_layout_sha256: str
    workspace_storage_scope_sha256: str
    active_mutation_run_sha256: str
    project_identity_sha256: str
    fit_material_sha256: str
    classifier_id: str
    classifier_kind: str
    request_identity_sha256: str
    request_envelope_sha256: str
    request_sha256: str
    result_bytes: bytes
    result_byte_count: int
    result_sha256: str
    script_sha256: str
    launch_nonce_sha256: str
    project_authority_head_before_sha256: str
    project_authority_head_after_sha256: str
    qpproj_before_sha256: str
    qpproj_after_sha256: str
    qpproj_byte_count: int
    native_classifier_before_sha256: str
    native_classifier_after_sha256: str
    native_classifier_byte_count: int
    request_ref: AuthorityMemberRefV2
    result_ref: AuthorityMemberRefV2
    stdout_ref: AuthorityMemberRefV2
    stderr_ref: AuthorityMemberRefV2
    _issuer_nonce: bytes
    _seal: str

    _PUBLIC_FIELDS: ClassVar[tuple[str, ...]] = (
        "authority_layout_sha256",
        "workspace_storage_scope_sha256",
        "active_mutation_run_sha256",
        "project_identity_sha256",
        "fit_material_sha256",
        "classifier_id",
        "classifier_kind",
        "request_identity_sha256",
        "request_envelope_sha256",
        "request_sha256",
        "result_bytes",
        "result_byte_count",
        "result_sha256",
        "script_sha256",
        "launch_nonce_sha256",
        "project_authority_head_before_sha256",
        "project_authority_head_after_sha256",
        "qpproj_before_sha256",
        "qpproj_after_sha256",
        "qpproj_byte_count",
        "native_classifier_before_sha256",
        "native_classifier_after_sha256",
        "native_classifier_byte_count",
        "request_ref",
        "result_ref",
        "stdout_ref",
        "stderr_ref",
    )

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise TypeError("native classifier read-only evidence is provider-issued")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("native classifier read-only evidence cannot be subclassed")

    def __copy__(self) -> NoReturn:
        raise TypeError("native classifier read-only evidence cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        del memo
        raise TypeError("native classifier read-only evidence cannot be copied")

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        del protocol
        raise TypeError("native classifier read-only evidence cannot be serialized")

    @classmethod
    def _issue(
        cls, *, provider: object, issuer: object, values: dict[str, object]
    ) -> "QuPathNativeClassifierReadOnlyEvidence":
        if issuer is not _READ_ONLY_EVIDENCE_ISSUER:
            raise ProjectAuthorityError("read-only evidence issuer is invalid")
        value = object.__new__(cls)
        for name in cls._PUBLIC_FIELDS:
            object.__setattr__(value, name, values[name])
        nonce = os.urandom(32)
        object.__setattr__(value, "_issuer_nonce", nonce)
        object.__setattr__(value, "_seal", _read_only_evidence_seal(value, nonce))
        identity = id(value)

        def discard(_reference: object) -> None:
            with _READ_ONLY_EVIDENCE_LOCK:
                _READ_ONLY_EVIDENCE_ISSUANCE.pop(identity, None)

        with _READ_ONLY_EVIDENCE_LOCK:
            _READ_ONLY_EVIDENCE_ISSUANCE[identity] = (
                weakref.ref(value, discard),
                weakref.ref(provider),
                nonce,
                value._seal,
            )
        return value


def _issue_native_classifier_read_only_evidence(
    *, provider: object, values: dict[str, object]
) -> QuPathNativeClassifierReadOnlyEvidence:
    return QuPathNativeClassifierReadOnlyEvidence._issue(
        provider=provider, issuer=_READ_ONLY_EVIDENCE_ISSUER, values=values
    )


def _authenticate_native_classifier_read_only_evidence_for_provider(
    provider: object, evidence: object
) -> QuPathNativeClassifierReadOnlyEvidence:
    if type(evidence) is not QuPathNativeClassifierReadOnlyEvidence:
        raise ProjectAuthorityError("read-only classifier evidence is foreign")
    try:
        with _READ_ONLY_EVIDENCE_LOCK:
            issued = _READ_ONLY_EVIDENCE_ISSUANCE.get(id(evidence))
        authenticated = (
            issued is not None
            and issued[0]() is evidence
            and issued[1]() is provider
            and issued[2] == evidence._issuer_nonce
            and issued[3] == evidence._seal
            and _read_only_evidence_seal(evidence, issued[2]) == issued[3]
        )
    except Exception:
        authenticated = False
    if not authenticated:
        raise ProjectAuthorityError("read-only classifier evidence authentication failed")
    return evidence
