"""Narrow retained authority-v2 capability for QuPath project records.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from functools import wraps
from threading import Lock
from typing import Callable, ParamSpec, TypeVar

from mif_pipeline.pipeline_core.authority_layout import (
    AuthorityLayoutError,
    AuthorityFamilyV2,
    AuthorityMemberRefV2,
    authority_member_ref_v2,
)
from mif_pipeline.pipeline_core.authority_provisioning import RetainedAuthorityLayoutV2
from mif_pipeline.pipeline_core.durable_records import (
    DurableRecordError,
    canonical_json_bytes,
)
from mif_pipeline.pipeline_core.runtime_lifecycle import (
    RuntimeLifecycleBorrower,
    RuntimeLifecycleOwner,
)

# ==============================================================================
#                               Project authority
# ==============================================================================


class ProjectAuthorityV2Error(ValueError):
    """A project member is outside the retained authority-v2 capability."""


_PROJECT_FAMILIES = frozenset(
    {
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
    }
)
_CAS_PAIRS = {
    "project_head_current": "project_head_history",
    "project_lease_current": "project_lease_record",
    "project_process_launch_current": "project_process_launch_receipt",
}

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _guarded_authority_operation(
    method: Callable[_P, _R],
) -> Callable[_P, _R]:
    @wraps(method)
    def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        authority = args[0]
        token = authority._runtime_lifecycle.acquire(  # type: ignore[attr-defined]
            "project-authority-operation"
        )
        try:
            return method(*args, **kwargs)
        finally:
            token.release()

    return guarded


class ProjectAuthorityV2:
    """Expose direct refs and JSON operations for one retained project layout."""

    __slots__ = (
        "_authority_layout_sha256",
        "_issued_derivations",
        "_issued_derivations_lock",
        "_retained",
        "_runtime_lifecycle",
        "_runtime_owner",
    )

    def __init__(
        self,
        retained: RetainedAuthorityLayoutV2,
        *,
        runtime_lifecycle: RuntimeLifecycleBorrower | None = None,
    ) -> None:
        if not isinstance(retained, RetainedAuthorityLayoutV2):
            raise ProjectAuthorityV2Error("project authority requires one retained v2 layout")
        try:
            retained.validate()
        except DurableRecordError as exc:
            raise ProjectAuthorityV2Error("retained project authority is unavailable") from exc
        self._retained = retained
        self._authority_layout_sha256 = retained.layout.layout_sha256
        self._runtime_owner = RuntimeLifecycleOwner.open() if runtime_lifecycle is None else None
        self._runtime_lifecycle = (
            self._runtime_owner.borrower if self._runtime_owner is not None else runtime_lifecycle
        )
        assert self._runtime_lifecycle is not None
        self._issued_derivations: dict[AuthorityMemberRefV2, tuple[bytes, bytes]] = {}
        self._issued_derivations_lock = Lock()

    @property
    def authority_layout_sha256(self) -> str:
        return self._authority_layout_sha256

    @property
    def is_quiescent(self) -> bool:
        return self._runtime_lifecycle.is_kind_quiescent("project-authority-operation")

    def authenticate_runtime_lifecycle(self, runtime_lifecycle: RuntimeLifecycleBorrower) -> None:
        if runtime_lifecycle is not self._runtime_lifecycle:
            raise ProjectAuthorityV2Error("project authority belongs to another runtime lifecycle")

    @_guarded_authority_operation
    def ref(
        self,
        family: str,
        *,
        scope: Mapping[str, object],
        natural_key: Mapping[str, object],
    ) -> AuthorityMemberRefV2:
        if type(family) is not str or family not in _PROJECT_FAMILIES:
            raise ProjectAuthorityV2Error("authority family is not a closed project family")
        try:
            scope_copy = dict(scope)
            natural_key_copy = dict(natural_key)
            ref = authority_member_ref_v2(
                self._retained.layout,
                family=family,
                scope=scope_copy,
                natural_key=natural_key_copy,
            )
            derivation = (
                canonical_json_bytes(scope_copy),
                canonical_json_bytes(natural_key_copy),
            )
        except (AuthorityLayoutError, DurableRecordError, KeyError, TypeError) as exc:
            raise ProjectAuthorityV2Error("project authority member key is invalid") from exc
        with self._issued_derivations_lock:
            prior = self._issued_derivations.setdefault(ref, derivation)
            if prior != derivation:
                raise ProjectAuthorityV2Error("project authority member derivation conflicts")
        return ref

    def _validate_cas_derivations(
        self,
        current: AuthorityMemberRefV2,
        immutable: AuthorityMemberRefV2,
    ) -> None:
        with self._issued_derivations_lock:
            current_derivation = self._issued_derivations.get(current)
            immutable_derivation = self._issued_derivations.get(immutable)
        if current_derivation is None or immutable_derivation is None:
            raise ProjectAuthorityV2Error(
                "project current/immutable refs were not issued by this authority"
            )
        current_scope, current_key = current_derivation
        immutable_scope, immutable_key = immutable_derivation
        if current_scope != immutable_scope:
            raise ProjectAuthorityV2Error("project current/immutable derivation scopes differ")
        if current.family in {"project_head_current", "project_lease_current"}:
            if current_key != b"{}":
                raise ProjectAuthorityV2Error("project current natural key is invalid")
        elif current.family == "project_process_launch_current":
            if current_key != immutable_key:
                raise ProjectAuthorityV2Error("project launch current/receipt roles differ")

    def _family_for(self, ref: AuthorityMemberRefV2) -> AuthorityFamilyV2:
        if (
            type(ref) is not AuthorityMemberRefV2
            or ref.authority_layout_sha256 != self.authority_layout_sha256
            or ref.family not in _PROJECT_FAMILIES
        ):
            raise ProjectAuthorityV2Error("project member belongs to another authority")
        family = self._retained.layout.family(ref.family)
        if ref.suffix != family.allowed_suffix or ref.media_type != family.media_type:
            raise ProjectAuthorityV2Error("project member media contract differs")
        return family

    def _json_family_for(self, ref: AuthorityMemberRefV2) -> AuthorityFamilyV2:
        family = self._family_for(ref)
        if family.media_type != "application/json":
            raise ProjectAuthorityV2Error("project member is not a JSON family")
        return family

    def _bucket_for(self, ref: AuthorityMemberRefV2):
        family = self._family_for(ref)
        return self._retained.bucket(family.physical_bucket)

    @_guarded_authority_operation
    def read_json(self, ref: AuthorityMemberRefV2, *, max_bytes: int) -> bytes:
        payload = self.read_json_optional(ref, max_bytes=max_bytes)
        if payload is None:
            raise ProjectAuthorityV2Error("project JSON member is missing")
        return payload

    @_guarded_authority_operation
    def read_json_optional(self, ref: AuthorityMemberRefV2, *, max_bytes: int) -> bytes | None:
        family = self._json_family_for(ref)
        if type(max_bytes) is not int or max_bytes <= 0 or max_bytes > family.max_member_bytes:
            raise ProjectAuthorityV2Error("project JSON read bound is invalid")
        try:
            payload = self._bucket_for(ref).read_immutable(
                ref.physical_name,
                max_bytes=max_bytes,
                label=f"{ref.family} project member",
                optional=True,
            )
            if payload is not None:
                family.validate_payload(payload)
            return payload
        except ProjectAuthorityV2Error:
            raise
        except (DurableRecordError, ValueError) as exc:
            raise ProjectAuthorityV2Error("project JSON member is unavailable") from exc

    @_guarded_authority_operation
    def publish_json(self, ref: AuthorityMemberRefV2, payload: bytes) -> None:
        family = self._json_family_for(ref)
        try:
            family.validate_payload(payload)
            self._bucket_for(ref).publish_immutable(
                ref.physical_name,
                payload,
                max_bytes=family.max_member_bytes,
                label=f"{ref.family} project member",
            )
            if self.read_json(ref, max_bytes=family.max_member_bytes) != payload:
                raise ProjectAuthorityV2Error("project JSON member changed after publication")
        except ProjectAuthorityV2Error:
            raise
        except (DurableRecordError, ValueError) as exc:
            raise ProjectAuthorityV2Error("project JSON publication conflicts") from exc

    @_guarded_authority_operation
    def cas(
        self,
        current: AuthorityMemberRefV2,
        immutable: AuthorityMemberRefV2,
        *,
        expected: bytes | None,
    ) -> bytes:
        current_family = self._json_family_for(current)
        immutable_family = self._json_family_for(immutable)
        if _CAS_PAIRS.get(current.family) != immutable.family:
            raise ProjectAuthorityV2Error("project current/immutable families do not pair")
        self._validate_cas_derivations(current, immutable)
        if expected is not None:
            current_family.validate_payload(expected)
        immutable_payload = self.read_json(immutable, max_bytes=immutable_family.max_member_bytes)
        try:
            replaced = self._bucket_for(current).replace_pointer_cas(
                current.physical_name,
                self._bucket_for(immutable),
                immutable.physical_name,
                expected_predecessor_bytes=expected,
                max_bytes=current_family.max_member_bytes,
                label=f"{current.family} project current",
            )
        except (DurableRecordError, ValueError) as exc:
            raise ProjectAuthorityV2Error("project current publication conflicts") from exc
        if (
            replaced != immutable_payload
            or self.read_json(current, max_bytes=current_family.max_member_bytes)
            != immutable_payload
        ):
            raise ProjectAuthorityV2Error("project current is not the exact immutable copy")
        return immutable_payload


__all__ = ["ProjectAuthorityV2", "ProjectAuthorityV2Error"]
