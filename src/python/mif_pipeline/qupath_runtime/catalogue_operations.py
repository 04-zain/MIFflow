"""Inspect and update the native QuPath project through registered runner operations.

The adapter builds bounded Groovy requests for project identity, image inventory, catalogue
changes and saved-state inspection. Returned evidence is parsed into pipeline records only
after checking the selected project path, native result files and image-role information.
The project owner decides when these operations are permitted."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from collections.abc import Mapping
from functools import wraps
from typing import Callable, ParamSpec, TypeVar

from mif_pipeline.pipeline_core.authority_layout import (
    AuthorityMemberRefV2,
    authority_member_ref_v2,
)
from mif_pipeline.pipeline_core.authority_provisioning import RetainedAuthorityLayoutV2
from mif_pipeline.pipeline_core.durable_records import (
    DurableRecordError,
    RuntimeAuthorityBindingV2,
)
from mif_pipeline.pipeline_core.errors import ProjectPreparationError
from mif_pipeline.pipeline_core.process_containment import (
    ContainmentLaunchRequest,
    LinuxLocalProcessGroupAttachmentV1,
    LinuxLocalProcessGroupDrainV1,
    LinuxSystemdScopeAttachmentV1,
    LinuxSystemdScopeDrainV1,
    WindowsKillOnCloseJobAttachmentV1,
    WindowsKillOnCloseJobDrainV1,
)
from mif_pipeline.pipeline_core.runtime_lifecycle import (
    RuntimeCompositionBinding,
    RuntimeLifecycleBorrower,
    RuntimeLifecycleOwner,
)
from mif_pipeline.pipeline_core.win32_file_authority import (
    Win32FileAuthority,
    Win32FileIdentity,
)

from ._script_asset_registry import QuPathScriptAsset
from .catalogue import (
    _add_bounded_cleanup_note,
    ProjectAuthorityError,
    ProjectMutationLease,
    ProjectMutationProcessDrainEvidence,
    QuPathCatalogueEvidenceEnvelope,
    QuPathCatalogueRequestEnvelope,
    QuPathProcessStartToken,
    QuPathProjectInspection,
    QuPathProjectInspectRequest,
    QuPathProjectSnapshot,
    canonical_project_uri,
)
from .runner import QuPathEvidenceRun, QuPathExecutionError
from .scripts import (
    ProjectStageScriptBundleAuthority,
    _PROJECT_STAGE_SCRIPT_BUNDLE_AUTHORITY_ISSUER,
    bundle_script_path,
    catalogue_runtime_authority_binding_v2,
    materialize_script_bundle,
    publish_registered_script_asset,
    registered_script_assets,
)
from .project import ProjectLayout
from .project_pyramid import (
    _ENTRY_OPTIONAL_FIELDS,
    _ENTRY_REQUIRED_FIELDS,
    _MAX_PROJECT_ENTRIES,
    _MAX_TAGS,
    _PROJECT_VERSION,
    _QPPROJ_FIELDS,
    _canonical_bytes as _pyramid_canonical_bytes,
    _closed as _pyramid_closed,
    _nonnegative_int as _pyramid_nonnegative_int,
    _parse_qpproj,
    _positive_int as _pyramid_positive_int,
    _same_file_uri,
    _text as _pyramid_text,
    _validate_server_builder_profile,
    ProjectEntryPyramidEvidence,
    ProjectEntryPyramidRequest,
    ProjectPyramidContractError,
    parse_project_entry_pyramid_payload,
    retained_project_file,
)
from .project_content import same_project_snapshot
from .registration_session import RegistrationProjectEntryPyramidObservation

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECOVERY_LOG_EXCERPT_MAX_BYTES = 65_536

_P = ParamSpec("_P")
_R = TypeVar("_R")


# ==============================================================================
#                              Catalogue operations
# ==============================================================================


def _guarded_project_operation(
    method: Callable[_P, _R],
) -> Callable[_P, _R]:
    @wraps(method)
    def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        operations = args[0]
        token = operations._runtime_lifecycle.acquire(  # type: ignore[attr-defined]
            "qupath-project-operation"
        )
        try:
            return method(*args, **kwargs)
        finally:
            token.release()

    return guarded


class _QuPathCatalogueOperationIssuerToken:
    """Identity-only token with no retained runtime or filesystem authority."""

    __slots__ = ()


class QuPathCatalogueOperationError(ProjectAuthorityError):
    """Private exact authority retained from one abnormal catalogue child."""

    __slots__ = (
        "_issuer_token",
        "request",
        "evidence",
        "request_ref",
        "evidence_ref",
        "process_start_token",
        "launch_evidence",
        "drain_evidence",
        "process_drain_sha256",
        "original_cause",
    )

    @classmethod
    def _issue(
        cls,
        *,
        issuer_token: object,
        request: QuPathCatalogueRequestEnvelope,
        evidence: QuPathCatalogueEvidenceEnvelope | None,
        request_ref: AuthorityMemberRefV2,
        evidence_ref: AuthorityMemberRefV2 | None,
        original_cause: QuPathExecutionError,
        process_drain: ProjectMutationProcessDrainEvidence | None,
    ) -> "QuPathCatalogueOperationError":
        if type(issuer_token) is not _QuPathCatalogueOperationIssuerToken or (
            process_drain is not None
            and not isinstance(process_drain, ProjectMutationProcessDrainEvidence)
        ):
            raise ProjectAuthorityError("abnormal catalogue operation authority is incomplete")
        process_start_token = (
            original_cause.process_start_token
            if process_drain is None
            else process_drain.process_start_token
        )
        drain_evidence = (
            original_cause.drain_evidence if process_drain is None else process_drain.drain_evidence
        )
        process_drain_sha256 = (
            original_cause.process_drain_sha256
            if process_drain is None
            else process_drain.process_drain_sha256
        )
        if (
            not isinstance(request, QuPathCatalogueRequestEnvelope)
            or type(request_ref) is not AuthorityMemberRefV2
            or request_ref.family != "runtime_exchange"
            or not isinstance(original_cause, QuPathExecutionError)
            or (evidence is None) != (evidence_ref is None)
            or (
                evidence_ref is not None
                and (
                    type(evidence_ref) is not AuthorityMemberRefV2
                    or evidence_ref.family != "runtime_exchange"
                    or evidence_ref.authority_layout_sha256 != request_ref.authority_layout_sha256
                    or evidence_ref == request_ref
                )
            )
            or (
                evidence is not None
                and (
                    not isinstance(evidence, QuPathCatalogueEvidenceEnvelope)
                    or evidence.request_envelope_sha256 != request.request_envelope_sha256
                    or evidence.launch_nonce_sha256 != request.launch_nonce_sha256
                    or evidence.process_start_token != process_start_token
                    or original_cause.launch_evidence is None
                    or drain_evidence is None
                    or process_drain_sha256 is None
                )
            )
            or (
                process_drain is not None
                and (
                    process_drain.process_start_token != original_cause.process_start_token
                    or process_drain.drain_evidence != original_cause.drain_evidence
                    or process_drain.launch_nonce_sha256 != request.launch_nonce_sha256
                    or process_drain.containment_drained is not True
                    or (
                        original_cause.process_drain_sha256 is not None
                        and process_drain.process_drain_sha256
                        != original_cause.process_drain_sha256
                    )
                )
            )
        ):
            raise ProjectAuthorityError("abnormal catalogue operation authority is incomplete")
        error = cls("QuPath catalogue operation retained abnormal transport")
        error._issuer_token = issuer_token
        error.request = request
        error.evidence = evidence
        error.request_ref = request_ref
        error.evidence_ref = evidence_ref
        error.process_start_token = process_start_token
        error.launch_evidence = original_cause.launch_evidence
        error.drain_evidence = drain_evidence
        error.process_drain_sha256 = process_drain_sha256
        error.original_cause = original_cause
        return error

    def _authenticated_pair(self, issuer_token: object) -> tuple[
        QuPathCatalogueRequestEnvelope,
        QuPathCatalogueEvidenceEnvelope | None,
    ]:
        if self._issuer_token is not issuer_token:
            raise ProjectAuthorityError("abnormal catalogue operation issuer is foreign")
        return self.request, self.evidence


def _new_win32_file_primitives() -> object:
    """Construct the shared native primitive lazily on Windows only."""

    from mif_pipeline.pipeline_core.win32_file_authority import (
        Win32FilePrimitives,
    )

    return Win32FilePrimitives()


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ProjectAuthorityError(f"{field} must be a lowercase SHA-256")
    return value


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
        raise ProjectAuthorityError("value is not canonical JSON") from exc


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_recovery_log_excerpt(raw: bytes, *, size: int) -> tuple[str, bool]:
    excerpt = raw.decode("utf-8", errors="replace")
    encoded = excerpt.encode("utf-8")
    representation_truncated = len(encoded) > _RECOVERY_LOG_EXCERPT_MAX_BYTES
    if representation_truncated:
        excerpt = encoded[-_RECOVERY_LOG_EXCERPT_MAX_BYTES:].decode("utf-8", errors="ignore")
    return (
        excerpt,
        size > _RECOVERY_LOG_EXCERPT_MAX_BYTES or representation_truncated,
    )


def _recover_contained_process_logs_windows(
    log_root: Path,
    request: ContainmentLaunchRequest,
    primitives: object,
) -> tuple[str, str, bool, bool]:
    directory: object | None = None
    handles: list[object] = []
    facts: list[tuple[str, bool]] = []
    primary: BaseException | None = None
    try:
        directory = primitives.open_directory(log_root, no_follow=True)
        if primitives.is_reparse_point(directory) or not primitives.is_directory(directory):
            raise ProjectAuthorityError("recovery log directory is not one safe directory")
        opened: list[tuple[str, object, tuple[int, str]]] = []
        for leaf in (request.stdout_log_name, request.stderr_log_name):
            try:
                handle = primitives.open_file(
                    directory,
                    leaf,
                    no_follow=True,
                    share_delete=False,
                )
                handles.append(handle)
                if (
                    primitives.is_reparse_point(handle)
                    or not primitives.is_regular_file(handle)
                    or primitives.link_count(handle) != 1
                ):
                    raise ProjectAuthorityError("recovery log leaf is not one safe regular file")
                opened.append((leaf, handle, primitives.identity(handle)))
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(f"additional recovery log open failure: {exc!r}"[:512])
        for _leaf, handle, _identity in opened:
            try:
                primitives.flush(handle)
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(f"additional recovery log fsync failure: {exc!r}"[:512])
        if primary is None:
            for leaf, handle, identity in opened:
                size = primitives.size(handle)
                if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    raise ProjectAuthorityError("recovery log size is invalid")
                raw = primitives.read_tail(handle, _RECOVERY_LOG_EXCERPT_MAX_BYTES)
                if (
                    not isinstance(raw, bytes)
                    or len(raw) > _RECOVERY_LOG_EXCERPT_MAX_BYTES
                    or primitives.is_reparse_point(handle)
                    or not primitives.is_regular_file(handle)
                    or primitives.link_count(handle) != 1
                    or primitives.size(handle) != size
                    or primitives.identity(handle) != identity
                    or primitives.child_identity(directory, leaf) != identity
                ):
                    raise ProjectAuthorityError("recovery log leaf changed while reading")
                facts.append(_bounded_recovery_log_excerpt(raw, size=size))
        if primary is not None:
            raise ProjectAuthorityError(
                "recovery logs could not be reopened after containment drain"
            ) from primary
        if len(facts) != 2:
            raise ProjectAuthorityError("recovery log evidence is incomplete")
        return facts[0][0], facts[1][0], facts[0][1], facts[1][1]
    except ProjectAuthorityError:
        raise
    except Exception as exc:
        raise ProjectAuthorityError("recovery log authority is unavailable") from exc
    finally:
        active_exception = sys.exc_info()[1]
        cleanup: BaseException | None = None
        for handle in reversed(handles):
            try:
                primitives.close(handle)
            except BaseException as exc:
                if cleanup is None:
                    cleanup = exc
                else:
                    cleanup.add_note(f"additional recovery log close failure: {exc!r}"[:512])
        if directory is not None:
            try:
                primitives.close(directory)
            except BaseException as exc:
                if cleanup is None:
                    cleanup = exc
                else:
                    cleanup.add_note(
                        f"additional recovery log directory close failure: {exc!r}"[:512]
                    )
        if cleanup is not None:
            if active_exception is not None:
                active_exception.add_note(f"recovery log cleanup failure: {cleanup!r}"[:512])
            else:
                raise ProjectAuthorityError(
                    "recovery log resources could not be closed"
                ) from cleanup


def _recover_contained_process_logs(
    log_root: Path,
    request: ContainmentLaunchRequest,
    *,
    _windows_primitives: object | None = None,
) -> tuple[str, str, bool, bool]:
    """Fsync and read the exact reserved log leaves after backend drain."""

    if type(request) is not ContainmentLaunchRequest:
        raise ProjectAuthorityError("recovery log authority is invalid")
    log_root = Path(log_root)
    if _windows_primitives is not None or os.name == "nt":
        try:
            primitives = (
                _windows_primitives
                if _windows_primitives is not None
                else _new_win32_file_primitives()
            )
        except Exception as exc:
            raise ProjectAuthorityError("recovery log authority is unavailable") from exc
        return _recover_contained_process_logs_windows(log_root, request, primitives)
    directory_descriptor: int | None = None
    descriptors: list[int] = []
    primary: BaseException | None = None
    facts: list[tuple[str, bool]] = []
    try:
        named = log_root.lstat()
        if (
            not stat.S_ISDIR(named.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or getattr(named, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ProjectAuthorityError("recovery log directory is not one safe directory")
        directory_descriptor = os.open(
            log_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_directory = os.fstat(directory_descriptor)
        if not stat.S_ISDIR(opened_directory.st_mode) or (
            opened_directory.st_dev,
            opened_directory.st_ino,
        ) != (named.st_dev, named.st_ino):
            raise ProjectAuthorityError("recovery log directory changed")

        opened_files: list[tuple[str, int, os.stat_result]] = []
        for leaf in (request.stdout_log_name, request.stderr_log_name):
            try:
                descriptor = os.open(
                    leaf,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_descriptor,
                )
                descriptors.append(descriptor)
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or getattr(opened, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                ):
                    raise ProjectAuthorityError("recovery log leaf is not one safe regular file")
                opened_files.append((leaf, descriptor, opened))
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(f"additional recovery log open failure: {exc!r}"[:512])
        for _leaf, descriptor, _opened in opened_files:
            try:
                os.fsync(descriptor)
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(f"additional recovery log fsync failure: {exc!r}"[:512])
        if primary is None:
            for leaf, descriptor, opened in opened_files:
                size = int(opened.st_size)
                os.lseek(
                    descriptor,
                    max(0, size - _RECOVERY_LOG_EXCERPT_MAX_BYTES),
                    os.SEEK_SET,
                )
                chunks: list[bytes] = []
                retained_bytes = 0
                while retained_bytes < _RECOVERY_LOG_EXCERPT_MAX_BYTES:
                    chunk = os.read(
                        descriptor,
                        _RECOVERY_LOG_EXCERPT_MAX_BYTES - retained_bytes,
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    retained_bytes += len(chunk)
                raw = b"".join(chunks)
                retained = os.fstat(descriptor)
                named_retained = os.stat(
                    leaf,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(retained.st_mode)
                    or retained.st_nlink != 1
                    or (retained.st_dev, retained.st_ino) != (opened.st_dev, opened.st_ino)
                    or (named_retained.st_dev, named_retained.st_ino)
                    != (opened.st_dev, opened.st_ino)
                ):
                    raise ProjectAuthorityError("recovery log leaf changed while reading")
                facts.append(_bounded_recovery_log_excerpt(raw, size=size))
        if primary is not None:
            raise ProjectAuthorityError(
                "recovery logs could not be reopened after containment drain"
            ) from primary
        if len(facts) != 2:
            raise ProjectAuthorityError("recovery log evidence is incomplete")
        return facts[0][0], facts[1][0], facts[0][1], facts[1][1]
    except OSError as exc:
        raise ProjectAuthorityError("recovery log authority is unavailable") from exc
    finally:
        active_exception = sys.exc_info()[1]
        cleanup: BaseException | None = None
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except BaseException as exc:
                if cleanup is None:
                    cleanup = exc
                else:
                    cleanup.add_note(f"additional recovery log close failure: {exc!r}"[:512])
        if directory_descriptor is not None:
            try:
                os.close(directory_descriptor)
            except BaseException as exc:
                if cleanup is None:
                    cleanup = exc
                else:
                    cleanup.add_note(
                        f"additional recovery log directory close failure: {exc!r}"[:512]
                    )
        if cleanup is not None:
            if active_exception is not None:
                active_exception.add_note(f"recovery log cleanup failure: {cleanup!r}"[:512])
            else:
                raise ProjectAuthorityError(
                    "recovery log resources could not be closed"
                ) from cleanup


class QuPathRunnerProjectOperations:
    """Bind typed catalogue operations to the one registered QuPath script."""

    def __init__(
        self,
        runner: object,
        *,
        authority_layout: RetainedAuthorityLayoutV2,
        workspace_storage_scope_sha256: str,
        runtime_lifecycle: RuntimeLifecycleBorrower | None = None,
        runtime_binding: RuntimeCompositionBinding | None = None,
    ) -> None:
        if not isinstance(authority_layout, RetainedAuthorityLayoutV2):
            raise ProjectAuthorityError("runtime retained authority is invalid")
        if (runtime_lifecycle is None) != (runtime_binding is None):
            raise ProjectAuthorityError(
                "runtime lifecycle and composition binding must be supplied together"
            )
        self._runner = runner
        self._authority_layout = authority_layout
        self._workspace_storage_scope_sha256 = _sha(
            workspace_storage_scope_sha256,
            "workspace_storage_scope_sha256",
        )
        self._runtime_owner = RuntimeLifecycleOwner.open() if runtime_lifecycle is None else None
        self._runtime_lifecycle = (
            self._runtime_owner.borrower if self._runtime_owner is not None else runtime_lifecycle
        )
        assert self._runtime_lifecycle is not None
        if self._runtime_owner is not None:
            self._composition_binding = self._runtime_owner.issue_binding(
                authority_layout=authority_layout,
                runner=runner,
                workspace_storage_scope_sha256=(self._workspace_storage_scope_sha256),
            )
        else:
            assert runtime_binding is not None
            self._runtime_lifecycle.authenticate_binding(
                runtime_binding,
                authority_layout=authority_layout,
                runner=runner,
                workspace_storage_scope_sha256=(self._workspace_storage_scope_sha256),
            )
            self._composition_binding = runtime_binding
        try:
            authority_layout.validate()
        except DurableRecordError as exc:
            raise ProjectAuthorityError("runtime retained authority is unavailable") from exc
        self._pending_launches: dict[str, tuple[object, object]] = {}
        self._project_stage_runtime_identity: object | None = None
        self._abnormal_operation_issuer_token = _QuPathCatalogueOperationIssuerToken()

    @property
    def workspace_storage_scope_sha256(self) -> str:
        return self._workspace_storage_scope_sha256

    @property
    def is_quiescent(self) -> bool:
        return self._runtime_lifecycle.is_kind_quiescent(
            "qupath-project-operation"
        ) and self._runtime_lifecycle.is_kind_quiescent("qupath-pending-launch")

    def authenticate_runtime_binding(
        self,
        binding: RuntimeCompositionBinding,
        *,
        runner: object,
    ) -> None:
        if binding is not self._composition_binding or runner is not self._runner:
            raise ProjectAuthorityError("runtime project-operations binding differs")
        self._runtime_lifecycle.authenticate_binding(
            binding,
            authority_layout=self._authority_layout,
            runner=runner,
            workspace_storage_scope_sha256=(self._workspace_storage_scope_sha256),
        )

    def _bind_project_stage_runtime_identity(
        self,
        runtime: object,
        *,
        binding: RuntimeCompositionBinding,
        runner: object,
    ) -> None:
        """Register the sole orchestration-issued facade for this composition."""

        self.authenticate_runtime_binding(binding, runner=runner)
        issued = self._project_stage_runtime_identity
        if issued is None:
            self._project_stage_runtime_identity = runtime
            return
        if issued is not runtime:
            raise ProjectAuthorityError(
                "project-stage runtime identity differs from the issued facade"
            )

    def _authenticate_project_stage_runtime_identity(
        self,
        runtime: object,
        *,
        binding: RuntimeCompositionBinding,
        runner: object,
    ) -> None:
        """Authenticate exact registered-facade object identity."""

        self.authenticate_runtime_binding(binding, runner=runner)
        if self._project_stage_runtime_identity is not runtime:
            raise ProjectAuthorityError("project-stage runtime identity is foreign")

    @staticmethod
    def _project_layout_for_stage(lease: ProjectMutationLease) -> ProjectLayout:
        project_file = lease.designation.project_path
        root = project_file.parent
        return ProjectLayout(
            root=root,
            project_file=project_file,
            scripts_dir=root / "scripts",
            pixel_classifiers_dir=root / "classifiers" / "pixel_classifiers",
            object_classifiers_dir=root / "classifiers" / "object_classifiers",
            data_dir=root / "data",
            exchange_dir=root / "exchange",
            checkpoints_dir=root / "checkpoints",
            provenance_dir=root / "provenance",
            runs_dir=root / "runs",
            config_path=root / "data" / "analysis-config.json",
            config_json="",
        )

    @_guarded_project_operation
    def materialize_project_stage_script_bundle(
        self,
        *,
        lease: ProjectMutationLease,
        target_asset: QuPathScriptAsset,
        guard_asset: QuPathScriptAsset,
    ) -> ProjectStageScriptBundleAuthority:
        """Issue exact reopened bundle facts for one live project-stage lease."""

        if (
            not isinstance(lease, ProjectMutationLease)
            or not lease.is_live
            or lease.operation != "project_stage"
            or not isinstance(target_asset, QuPathScriptAsset)
            or not isinstance(guard_asset, QuPathScriptAsset)
            or target_asset is guard_asset
            or target_asset is QuPathScriptAsset.BATCH_STAGE_GUARD
            or guard_asset is not QuPathScriptAsset.BATCH_STAGE_GUARD
            or target_asset not in registered_script_assets()
        ):
            raise ProjectAuthorityError(
                "project-stage script bundle requires its exact live lease and assets"
            )
        provider = lease._provider
        try:
            active = provider._validate_live_lease_authority(lease, require_project=True)
            active_sha256 = provider._active_mutation_run_sha256(active)
            workspace_scope = provider._workspace_storage_scope_sha256(lease)
            if (
                workspace_scope != self.workspace_storage_scope_sha256
                or lease.project_authority.authority_layout_sha256
                != self._authority_layout.layout.layout_sha256
                or canonical_project_uri(lease.designation.project_path)
                != lease.designation.project_uri
            ):
                raise ProjectAuthorityError("project-stage script bundle authority differs")
            project = self._project_layout_for_stage(lease)
            bundle = materialize_script_bundle(project, registered_script_assets())
            manifest_bytes = (bundle.root / "qupath-script-bundle.v1.json").read_bytes()
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            if (
                manifest_sha256 != bundle.bundle_sha256
                or bundle.assets != registered_script_assets()
                or tuple(asset for asset, _digest in bundle.asset_sha256s)
                != registered_script_assets()
            ):
                raise ProjectAuthorityError("project-stage complete script bundle differs")
            target_bundle_bytes = bundle_script_path(bundle, target_asset).read_bytes()
            guard_bundle_bytes = bundle_script_path(bundle, guard_asset).read_bytes()
            asset_digests = dict(bundle.asset_sha256s)
            if (
                hashlib.sha256(target_bundle_bytes).hexdigest() != asset_digests[target_asset]
                or hashlib.sha256(guard_bundle_bytes).hexdigest() != asset_digests[guard_asset]
            ):
                raise ProjectAuthorityError("project-stage script bundle member differs")

            target_ref = publish_registered_script_asset(self._authority_layout, target_asset)
            guard_ref = publish_registered_script_asset(self._authority_layout, guard_asset)
            immutable = self._authority_layout.bucket("immutable")
            target_bytes = immutable.read_member(target_ref)
            guard_bytes = immutable.read_member(guard_ref)
            if target_bytes != target_bundle_bytes or guard_bytes != guard_bundle_bytes:
                raise ProjectAuthorityError("project-stage retained script bytes differ")
            manifest_ref = authority_member_ref_v2(
                self._authority_layout.layout,
                family="batch_project_runtime_evidence",
                scope={
                    "workspace_storage_scope_sha256": workspace_scope,
                },
                natural_key={"evidence_sha256": manifest_sha256},
            )
            immutable.publish_member(manifest_ref, manifest_bytes)
            reopened_manifest = immutable.read_member(manifest_ref)
            if reopened_manifest != manifest_bytes:
                raise ProjectAuthorityError("project-stage retained script manifest differs")
            provider._validate_live_lease_authority(lease, require_project=True)
            return ProjectStageScriptBundleAuthority._issue(
                authority_layout_sha256=(self._authority_layout.layout.layout_sha256),
                workspace_storage_scope_sha256=workspace_scope,
                active_mutation_run_sha256=active_sha256,
                project_uri=lease.designation.project_uri,
                project_identity_sha256=(lease.designation.identity.project_identity_sha256),
                script_bundle_manifest_ref=manifest_ref,
                script_bundle_manifest_bytes=reopened_manifest,
                target_script_asset=target_asset,
                target_script_ref=target_ref,
                target_script_bytes=target_bytes,
                guard_script_asset=guard_asset,
                guard_script_ref=guard_ref,
                guard_script_bytes=guard_bytes,
                _issuer=_PROJECT_STAGE_SCRIPT_BUNDLE_AUTHORITY_ISSUER,
            )
        except ProjectAuthorityError:
            raise
        except (DurableRecordError, ProjectPreparationError, OSError) as exc:
            raise ProjectAuthorityError(
                "project-stage script bundle cannot be authenticated"
            ) from exc

    def _authenticate_abnormal_operation_error(self, error: QuPathCatalogueOperationError) -> tuple[
        QuPathCatalogueRequestEnvelope,
        QuPathCatalogueEvidenceEnvelope | None,
    ]:
        if type(error) is not QuPathCatalogueOperationError:
            raise ProjectAuthorityError("abnormal catalogue operation error is not authentic")
        return error._authenticated_pair(self._abnormal_operation_issuer_token)

    @_guarded_project_operation
    def issue_project_mutation_launch(self, *, lease: ProjectMutationLease) -> object:
        issuer = getattr(self._runner, "issue_project_mutation_launch", None)
        if not callable(issuer) or not isinstance(lease, ProjectMutationLease) or not lease.is_live:
            raise ProjectAuthorityError("live runner launch authority is required")
        launch = issuer(project_lease=lease)
        digest = _sha(
            getattr(launch, "launch_nonce_sha256", None),
            "launch_nonce_sha256",
        )
        if digest in self._pending_launches:
            raise ProjectAuthorityError("runner reused a pending project launch nonce")
        pending = self._runtime_lifecycle.acquire("qupath-pending-launch")
        self._pending_launches[digest] = (launch, pending)
        return launch

    @_guarded_project_operation
    def _discard_unlaunched_project_replay(
        self,
        request: QuPathCatalogueRequestEnvelope,
        *,
        lease: ProjectMutationLease,
    ) -> None:
        """Discard an ephemeral launch only after provider absence proof."""

        if (
            not isinstance(request, QuPathCatalogueRequestEnvelope)
            or request.launch_nonce_sha256 is None
            or not isinstance(lease, ProjectMutationLease)
            or not lease.is_live
        ):
            raise ProjectAuthorityError("unlaunched project replay authority is invalid")
        pending = self._pending_launches.pop(request.launch_nonce_sha256, None)
        launch = None if pending is None else pending[0]
        if pending is not None:
            pending[1].release()
        if launch is not None and hasattr(launch, "_raw_nonce"):
            object.__setattr__(launch, "_raw_nonce", b"")

    @_guarded_project_operation
    def inspect_project(
        self,
        project_path: Path,
        *,
        require_opened_file_identity: bool = False,
    ) -> QuPathProjectSnapshot | QuPathProjectInspection:
        descriptor: int | None = None
        opened_identity: tuple[int, int] | Win32FileIdentity | None = None
        win32_authority: Win32FileAuthority | None = None
        win32_parent: object | None = None
        win32_target: object | None = None
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

        def stable_file_state(value: os.stat_result) -> tuple[int, ...]:
            return (
                int(stat.S_IFMT(value.st_mode)),
                int(value.st_dev),
                int(value.st_ino),
                int(value.st_size),
                int(value.st_nlink),
                int(value.st_mtime_ns),
                int(value.st_ctime_ns),
                int(getattr(value, "st_file_attributes", 0)) & reparse_flag,
            )

        if require_opened_file_identity:
            if os.name == "nt":
                try:
                    win32_authority = Win32FileAuthority()
                    win32_parent = win32_authority.retain_directory(
                        Path(project_path).parent.resolve(strict=True),
                        create=False,
                    )
                    win32_target = win32_authority.retain_mutable_target(
                        win32_parent,
                        Path(project_path).name,
                    )
                    opened_identity = win32_authority.mutable_target_identity(
                        win32_target,
                        allow_unlinked_predecessor=False,
                    )
                except Exception as exc:
                    primary = ProjectAuthorityError(
                        "project inspector cannot retain the opened file identity"
                    )
                    if win32_authority is not None and win32_target is not None:
                        try:
                            win32_authority.close_mutable_target(win32_target)
                        except BaseException as cleanup:
                            _add_bounded_cleanup_note(
                                primary,
                                cleanup,
                                context="project inspector HANDLE cleanup failed",
                            )
                    if win32_authority is not None and win32_parent is not None:
                        try:
                            win32_authority.close_directory(win32_parent)
                        except BaseException as cleanup:
                            _add_bounded_cleanup_note(
                                primary,
                                cleanup,
                                context="project inspector parent cleanup failed",
                            )
                    raise primary from exc
            else:
                try:
                    named = Path(project_path).lstat()
                    descriptor = os.open(
                        project_path,
                        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    )
                    opened = os.fstat(descriptor)
                except OSError as exc:
                    if descriptor is not None:
                        os.close(descriptor)
                    raise ProjectAuthorityError(
                        "project inspector cannot retain the opened file identity"
                    ) from exc
                if (
                    not stat.S_ISREG(named.st_mode)
                    or not stat.S_ISREG(opened.st_mode)
                    or named.st_nlink != 1
                    or opened.st_nlink != 1
                    or getattr(named, "st_file_attributes", 0) & reparse_flag
                    or getattr(opened, "st_file_attributes", 0) & reparse_flag
                    or stable_file_state(named) != stable_file_state(opened)
                ):
                    os.close(descriptor)
                    descriptor = None
                    raise ProjectAuthorityError("project inspector opened an unsafe file identity")
                opened_identity = (int(opened.st_dev), int(opened.st_ino))
        request = QuPathCatalogueRequestEnvelope.create(
            operation="inspect_project",
            project_identity_sha256=None,
            mutation_request_sha256=None,
            mutation_authorization_sha256=None,
            fencing_token_sha256=None,
            launch_nonce_sha256=None,
            payload=QuPathProjectInspectRequest(None),
        )
        try:
            evidence = self._execute(project_path, request, lease=None, launch=None)
            if opened_identity is None:
                return evidence.post_save_project_snapshot
            if os.name == "nt":
                assert win32_authority is not None
                assert win32_parent is not None
                assert win32_target is not None
                if (
                    win32_authority.mutable_target_identity(
                        win32_target,
                        allow_unlinked_predecessor=False,
                    )
                    != opened_identity
                ):
                    raise ProjectAuthorityError(
                        "project inspector file identity changed during inspection"
                    )
                win32_authority.validate_mutable_target_name(
                    win32_parent,
                    Path(project_path).name,
                    win32_target,
                )
                return QuPathProjectInspection(
                    evidence.post_save_project_snapshot,
                    opened_identity,
                )
            assert descriptor is not None
            retained = os.fstat(descriptor)
            named_after = Path(project_path).lstat()
            if stable_file_state(retained) != stable_file_state(opened) or stable_file_state(
                named_after
            ) != stable_file_state(opened):
                raise ProjectAuthorityError(
                    "project inspector file identity changed during inspection"
                )
            return QuPathProjectInspection(
                evidence.post_save_project_snapshot,
                opened_identity,
            )
        except OSError as exc:
            raise ProjectAuthorityError(
                "project inspector file identity cannot be revalidated"
            ) from exc
        finally:
            primary = sys.exc_info()[1]
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException as cleanup:
                    if primary is not None:
                        _add_bounded_cleanup_note(
                            primary,
                            cleanup,
                            context="project inspector descriptor cleanup failed",
                        )
                    elif isinstance(cleanup, Exception):
                        raise ProjectAuthorityError(
                            "project inspector descriptor cleanup failed"
                        ) from cleanup
                    else:
                        raise
            if win32_authority is not None and win32_target is not None:
                try:
                    win32_authority.close_mutable_target(win32_target)
                except BaseException as cleanup:
                    if primary is not None:
                        _add_bounded_cleanup_note(
                            primary,
                            cleanup,
                            context="project inspector HANDLE cleanup failed",
                        )
                    elif isinstance(cleanup, Exception):
                        primary = ProjectAuthorityError("project inspector HANDLE cleanup failed")
                        primary.__cause__ = cleanup
                    else:
                        raise
            if win32_authority is not None and win32_parent is not None:
                try:
                    win32_authority.close_directory(win32_parent)
                except BaseException as cleanup:
                    if primary is not None:
                        _add_bounded_cleanup_note(
                            primary,
                            cleanup,
                            context="project inspector parent cleanup failed",
                        )
                    elif isinstance(cleanup, Exception):
                        raise ProjectAuthorityError(
                            "project inspector parent cleanup failed"
                        ) from cleanup
                    else:
                        raise
            if primary is not None and sys.exc_info()[1] is None:
                raise primary

    @_guarded_project_operation
    def ensure_project_identity(
        self,
        project_path: Path,
        request: QuPathCatalogueRequestEnvelope,
        *,
        lease: ProjectMutationLease,
    ) -> QuPathCatalogueEvidenceEnvelope:
        if request.operation != "ensure_project_identity":
            raise ProjectAuthorityError("identity operation received the wrong request")
        return self._execute_mutation(project_path, request, lease=lease)

    @_guarded_project_operation
    def upsert_entry(
        self,
        project_path: Path,
        request: QuPathCatalogueRequestEnvelope,
        *,
        lease: ProjectMutationLease,
    ) -> QuPathCatalogueEvidenceEnvelope:
        if request.operation != "upsert_entry":
            raise ProjectAuthorityError("upsert operation received the wrong request")
        return self._execute_mutation(project_path, request, lease=lease)

    @_guarded_project_operation
    def reopen_completed_catalogue_evidence(
        self,
        project_path: Path,
        request: QuPathCatalogueRequestEnvelope,
        *,
        lease: ProjectMutationLease,
        process_drain: ProjectMutationProcessDrainEvidence,
    ) -> QuPathCatalogueEvidenceEnvelope:
        """Read a completed exchange under its original request and normal drain.

        We reuse the native result only when its request, process and current
        project bytes agree with the live lease. This operation launches no
        process and publishes no replacement runtime or terminal record.
        """
        native_project = Path(project_path).resolve(strict=False)
        if (not isinstance(lease, ProjectMutationLease)
                or native_project != lease.designation.project_path.resolve(strict=False)
                or request.project_identity_sha256 != lease.designation.identity.project_identity_sha256
                or request.mutation_request_sha256 != lease.mutation_request_sha256
                or request.mutation_authorization_sha256 != lease.mutation_authorization_sha256
                or request.fencing_token_sha256 != lease.fencing_token_sha256):
            raise ProjectAuthorityError("completed catalogue request differs from its lease")
        binding = self._runtime_binding(request, project_path=native_project, lease=lease)
        exchange = self._authority_layout.bucket("exchange")
        try:
            if exchange.read_member(binding.request_ref) != _canonical_bytes(request.to_mapping()):
                raise ProjectAuthorityError("completed catalogue request bytes changed")
            evidence = QuPathCatalogueEvidenceEnvelope.from_mapping(
                json.loads(exchange.read_member(binding.evidence_ref).decode("utf-8"))
            )
        except (DurableRecordError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectAuthorityError("completed catalogue evidence is unreadable") from exc
        self._validate_evidence_payload(native_project, request, evidence)
        if (not isinstance(process_drain, ProjectMutationProcessDrainEvidence)
                or process_drain.launch_nonce_sha256 != request.launch_nonce_sha256
                or process_drain.process_start_token != evidence.process_start_token
                or not lease._provider._is_success_process_drain(lease, process_drain)):
            raise ProjectAuthorityError("completed catalogue evidence lacks its original normal drain")
        return evidence

    def _execute_mutation(
        self,
        project_path: Path,
        request: QuPathCatalogueRequestEnvelope,
        *,
        lease: ProjectMutationLease,
    ) -> QuPathCatalogueEvidenceEnvelope:
        if (
            not isinstance(lease, ProjectMutationLease)
            or not lease.is_live
            or request.project_identity_sha256 != lease.designation.identity.project_identity_sha256
            or request.mutation_request_sha256 != lease.mutation_request_sha256
            or request.mutation_authorization_sha256 != lease.mutation_authorization_sha256
            or request.fencing_token_sha256 != lease.fencing_token_sha256
        ):
            raise ProjectAuthorityError("catalogue request disagrees with its live lease")
        digest = _sha(request.launch_nonce_sha256, "launch_nonce_sha256")
        pending = self._pending_launches.pop(digest, None)
        if pending is None:
            raise ProjectAuthorityError("catalogue request lacks its runner-issued launch")
        launch, pending_token = pending
        pending_token.release()
        return self._execute(project_path, request, lease=lease, launch=launch)

    def _runtime_binding(
        self,
        request: QuPathCatalogueRequestEnvelope,
        *,
        project_path: Path,
        lease: ProjectMutationLease | None,
    ) -> RuntimeAuthorityBindingV2:
        if lease is None:
            script_ref = publish_registered_script_asset(
                self._authority_layout,
                QuPathScriptAsset.UPSERT_BATCH_PROJECT_ENTRY,
            )
            try:
                project_sha256 = _file_sha256(Path(project_path))
            except OSError as exc:
                raise ProjectAuthorityError("inspection project bytes are unavailable") from exc
            inspection_generation_sha256 = hashlib.sha256(os.urandom(32)).hexdigest()
            active_run_sha256 = hashlib.sha256(
                _canonical_bytes(
                    {
                        "inspection_generation_sha256": inspection_generation_sha256,
                        "inspection_request_sha256": (request.request_envelope_sha256),
                        "project_sha256": project_sha256,
                        "script_ref_sha256": script_ref.key_sha256,
                    }
                )
            ).hexdigest()
            launch_nonce_sha256 = active_run_sha256
            role = "identity_materialization"
        else:
            active = lease._provider._validate_live_lease_authority(lease, require_project=False)
            active_run_sha256 = lease._provider._active_mutation_run_sha256(active)
            launch_nonce_sha256 = _sha(request.launch_nonce_sha256, "launch_nonce_sha256")
            role = lease._provider._runtime_request_role(request)
        return catalogue_runtime_authority_binding_v2(
            self._authority_layout,
            workspace_storage_scope_sha256=self.workspace_storage_scope_sha256,
            active_mutation_run_sha256=active_run_sha256,
            role=role,
            request_identity_sha256=request.request_envelope_sha256,
            launch_nonce_sha256=launch_nonce_sha256,
        )

    @_guarded_project_operation
    def inspect_project_entry_pyramid(
        self,
        project_path: Path,
        request: ProjectEntryPyramidRequest,
    ) -> ProjectEntryPyramidEvidence:
        """Parse one exact qpproj metadata projection without launching QuPath."""

        if type(request) is not ProjectEntryPyramidRequest:
            raise ProjectPyramidContractError(
                "project-entry pyramid inspection requires one exact request"
            )
        native_project = Path(os.path.abspath(os.fspath(project_path)))
        try:
            if canonical_project_uri(native_project) != request.project_uri:
                raise ProjectPyramidContractError(
                    "project-entry pyramid request names a different project"
                )
            with retained_project_file(native_project) as retained:
                if not same_project_snapshot(
                    native_project, request.qpproj_snapshot_sha256, retained.sha256,
                    observed_payload=retained.payload,
                ):
                    raise ProjectPyramidContractError(
                        "project-entry pyramid qpproj snapshot differs"
                    )
                observation = parse_project_entry_pyramid_payload(
                    retained.payload,
                    project_uri=request.project_uri,
                    project_entry_id=request.project_entry_id,
                    batch_id=request.batch_id,
                    sample_id=request.sample_id,
                    slide_id=request.slide_id,
                    validation_domain_id=request.validation_domain_id,
                    canonical_image_uri=request.canonical_image_uri,
                    canonical_image_sha256=request.canonical_image_sha256,
                    level0_size_x_px=request.level0_size_x_px,
                    level0_size_y_px=request.level0_size_y_px,
                    frame_x_um_per_px=float.fromhex(request.frame_x_um_per_px_hex),
                    frame_y_um_per_px=float.fromhex(request.frame_y_um_per_px_hex),
                )
                if (
                    observation.qpproj_snapshot_sha256 != retained.sha256
                    or observation.parser_profile_id != request.parser_profile_id
                    or observation.project_version != request.project_version
                    or observation.server_builder_json != request.server_builder_json
                    or observation.server_builder_sha256 != request.server_builder_sha256
                    or observation.server_builder_metadata_sha256
                    != request.server_builder_metadata_sha256
                ):
                    raise ProjectPyramidContractError(
                        "project-entry pyramid retained metadata differs from its request"
                    )
                evidence = ProjectEntryPyramidEvidence.create(
                    request=request,
                    physical_calibration=observation.physical_calibration,
                    native_pyramid_inventory=(observation.native_pyramid_inventory),
                )
                evidence.validate_request_binding(request)
                retained.validate_unchanged()
                return evidence
        except ProjectPyramidContractError:
            raise
        except (
            ProjectAuthorityError,
            UnicodeError,
            OSError,
        ) as exc:
            raise ProjectPyramidContractError(
                "project-entry pyramid inspection could not be authenticated"
            ) from exc

    @_guarded_project_operation
    def inspect_registration_project_entry_pyramid(
        self,
        project_path: Path,
        *,
        entry_role: str,
        entry: object,
    ) -> RegistrationProjectEntryPyramidObservation:
        """Observe one fixed/moving entry from retained qpproj bytes only."""

        from .catalogue import QuPathProjectEntryEvidence

        expected_image_role = {
            "fixed": "primary_mif",
            "moving": "moving_reference",
        }.get(entry_role)
        if expected_image_role is None or type(entry) is not QuPathProjectEntryEvidence:
            raise ProjectPyramidContractError(
                "registration pyramid inspection requires one exact entry role"
            )
        native_project = Path(os.path.abspath(os.fspath(project_path)))
        try:
            project_uri = canonical_project_uri(native_project)
            with retained_project_file(native_project) as retained:
                root = _pyramid_closed(_parse_qpproj(retained.payload), _QPPROJ_FIELDS, "qpproj")
                if root["version"] != _PROJECT_VERSION:
                    raise ProjectPyramidContractError("registration qpproj version is unsupported")
                _same_file_uri(root["uri"], project_uri, "qpproj project URI")
                _pyramid_nonnegative_int(root["createTimestamp"], "createTimestamp")
                _pyramid_nonnegative_int(root["modifyTimestamp"], "modifyTimestamp")
                last_id = _pyramid_nonnegative_int(root["lastID"], "lastID")
                images = root["images"]
                if not isinstance(images, list) or len(images) > _MAX_PROJECT_ENTRIES:
                    raise ProjectPyramidContractError(
                        "registration qpproj exceeds its project entry bound"
                    )
                if not images:
                    raise ProjectPyramidContractError("registration qpproj has no entries")
                matches: list[Mapping[str, object]] = []
                seen: set[int] = set()
                for raw in images:
                    if not isinstance(raw, Mapping):
                        raise ProjectPyramidContractError("qpproj entry must be an object")
                    fields = frozenset(raw)
                    if not (
                        _ENTRY_REQUIRED_FIELDS
                        <= fields
                        <= _ENTRY_REQUIRED_FIELDS | _ENTRY_OPTIONAL_FIELDS
                    ):
                        raise ProjectPyramidContractError("qpproj entry requires exact fields")
                    entry_id = _pyramid_positive_int(raw["entryID"], "entryID")
                    if entry_id in seen:
                        raise ProjectPyramidContractError("qpproj duplicates entryID")
                    seen.add(entry_id)
                    if str(entry_id) == entry.project_entry_id:
                        matches.append(raw)
                if last_id < max(seen):
                    raise ProjectPyramidContractError("qpproj lastID precedes a project entry")
                if len(matches) != 1:
                    raise ProjectPyramidContractError(
                        "registration qpproj does not contain the exact entry"
                    )
                raw_entry = matches[0]
                if "description" in raw_entry and raw_entry["description"] is not None:
                    _pyramid_text(raw_entry["description"], "description")
                tags = raw_entry["tags"]
                if (
                    not isinstance(tags, list)
                    or len(tags) > _MAX_TAGS
                    or any(type(tag) is not str or not tag for tag in tags)
                    or len(set(tags)) != len(tags)
                ):
                    raise ProjectPyramidContractError("qpproj entry tags are invalid")
                _pyramid_text(raw_entry["randomizedName"], "randomizedName")
                if (
                    _pyramid_text(raw_entry["imageName"], "imageName") != entry.entry_name
                    or tuple(entry.source_uris) == ()
                    or len(entry.source_uris) != 1
                ):
                    raise ProjectPyramidContractError(
                        "registration entry projection differs from qpproj"
                    )
                entry_metadata = raw_entry["metadata"]
                if not isinstance(entry_metadata, Mapping) or any(
                    type(key) is not str or type(value) is not str
                    for key, value in entry_metadata.items()
                ):
                    raise ProjectPyramidContractError("registration entry metadata is invalid")
                required = {
                    "mif_pipeline.image_role",
                    "mif_pipeline.source_image_sha256",
                    "mif_pipeline.batch_id",
                    "mif_pipeline.sample_id",
                    "mif_pipeline.slide_id",
                    "mif_pipeline.validation_domain_id",
                }
                expected_fields = required | (
                    {"mif_pipeline.reference_modality"} if entry_role == "moving" else set()
                )
                if (
                    set(entry_metadata) != expected_fields
                    or dict(entry_metadata) != dict(entry.metadata)
                    or entry_metadata["mif_pipeline.image_role"] != expected_image_role
                ):
                    raise ProjectPyramidContractError(
                        "registration entry metadata differs from its exact role"
                    )
                source_uri = entry.source_uris[0]
                builder = raw_entry["serverBuilder"]
                if not isinstance(builder, Mapping) or not isinstance(
                    builder.get("metadata"), Mapping
                ):
                    raise ProjectPyramidContractError("registration serverBuilder is invalid")
                metadata = builder["metadata"]
                calibration = metadata.get("pixelCalibration")
                if not isinstance(calibration, Mapping):
                    raise ProjectPyramidContractError("registration calibration is invalid")
                width = _pyramid_positive_int(metadata.get("width"), "width")
                height = _pyramid_positive_int(metadata.get("height"), "height")
                pixel_width = calibration.get("pixelWidth")
                pixel_height = calibration.get("pixelHeight")
                if not isinstance(pixel_width, Mapping) or not isinstance(pixel_height, Mapping):
                    raise ProjectPyramidContractError("registration calibration axes are invalid")
                x_um = float(pixel_width.get("value"))
                y_um = float(pixel_height.get("value"))
                profile = _validate_server_builder_profile(
                    builder,
                    canonical_image_uri=source_uri,
                    level0_size_x_px=width,
                    level0_size_y_px=height,
                    frame_x_um_per_px=x_um,
                    frame_y_um_per_px=y_um,
                    expected_image_name=entry.entry_name,
                    image_role=expected_image_role,
                )
                observation = RegistrationProjectEntryPyramidObservation.create(
                    entry_role=entry_role,
                    image_role=expected_image_role,
                    project_uri=project_uri,
                    qpproj_sha256=retained.sha256,
                    project_entry_id=entry.project_entry_id,
                    entry_projection_sha256=entry.entry_projection_sha256,
                    source_uri=source_uri,
                    source_image_sha256=entry_metadata["mif_pipeline.source_image_sha256"],
                    server_builder_metadata_sha256=hashlib.sha256(
                        _pyramid_canonical_bytes(profile.metadata)
                    ).hexdigest(),
                    native_pyramid_inventory_sha256=hashlib.sha256(
                        _pyramid_canonical_bytes(
                            [dict(item) for item in profile.native_pyramid_inventory]
                        )
                    ).hexdigest(),
                    level0_size_x_px=width,
                    level0_size_y_px=height,
                    x_um_per_px_hex=x_um.hex(),
                    y_um_per_px_hex=y_um.hex(),
                    channel_names=tuple(channel["name"] for channel in profile.metadata["channels"]),
                    is_rgb=profile.metadata["isRGB"],
                )
                retained.validate_unchanged()
                return observation
        except ProjectPyramidContractError:
            raise
        except (OSError, TypeError, ValueError, UnicodeError) as exc:
            raise ProjectPyramidContractError(
                "registration pyramid inspection could not be authenticated"
            ) from exc

    def _execute(
        self,
        project_path: Path,
        request: QuPathCatalogueRequestEnvelope,
        *,
        lease: ProjectMutationLease | None,
        launch: object | None,
    ) -> QuPathCatalogueEvidenceEnvelope:
        native_project = Path(project_path).resolve(strict=False)
        if lease is not None and native_project != lease.designation.project_path.resolve(
            strict=False
        ):
            raise ProjectAuthorityError("catalogue project path disagrees with its lease")
        binding = self._runtime_binding(request, project_path=native_project, lease=lease)
        exchange = self._authority_layout.bucket("exchange")
        request_bytes = _canonical_bytes(request.to_mapping())
        try:
            exchange.publish_member(binding.request_ref, request_bytes)
            try:
                if lease is None:
                    run = self._runner.run_authority_evidence_script(
                        project_file=native_project,
                        authority_layout=self._authority_layout,
                        binding=binding,
                        require_existing_project=True,
                    )
                else:
                    run = self._runner.run_project_evidence_script(
                        project_file=native_project,
                        authority_layout=self._authority_layout,
                        binding=binding,
                        project_lease=lease,
                        launch=launch,
                    )
            except QuPathExecutionError as exc:
                if lease is not None:
                    try:
                        process_drain = self._retain_exceptional_process_drain(
                            request,
                            lease=lease,
                            error=exc,
                        )
                        reopened_request = exchange.read_member(binding.request_ref)
                        if reopened_request != request_bytes:
                            raise ProjectAuthorityError("abnormal catalogue request bytes changed")
                        evidence_bytes = exchange.read_member(binding.evidence_ref, optional=True)
                        evidence = None
                        evidence_ref = None
                        if evidence_bytes is not None:
                            evidence = QuPathCatalogueEvidenceEnvelope.from_mapping(
                                json.loads(evidence_bytes.decode("utf-8"))
                            )
                            self._validate_abnormal_evidence(
                                native_project,
                                request,
                                evidence,
                                error=exc,
                                process_drain=process_drain,
                                lease=lease,
                            )
                            evidence_ref = binding.evidence_ref
                    except BaseException as barrier_error:
                        barrier_error.add_note(f"QuPath exceptional result was: {exc!r}"[:512])
                        raise
                    operation_error = QuPathCatalogueOperationError._issue(
                        issuer_token=self._abnormal_operation_issuer_token,
                        request=request,
                        evidence=evidence,
                        request_ref=binding.request_ref,
                        evidence_ref=evidence_ref,
                        original_cause=exc,
                        process_drain=process_drain,
                    )
                    raise operation_error from exc
                raise
            evidence_bytes = exchange.read_member(binding.evidence_ref)
            if type(evidence_bytes) is not bytes:
                raise ProjectAuthorityError("QuPath catalogue evidence is unavailable")
            evidence = QuPathCatalogueEvidenceEnvelope.from_mapping(
                json.loads(evidence_bytes.decode("utf-8"))
            )
            self._validate_evidence(
                native_project,
                request,
                evidence,
                run=run,
                lease=lease,
            )
            return evidence
        except (DurableRecordError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectAuthorityError("QuPath catalogue evidence is unreadable") from exc

    @staticmethod
    def _retain_exceptional_process_drain(
        request: QuPathCatalogueRequestEnvelope,
        *,
        lease: ProjectMutationLease,
        error: QuPathExecutionError,
    ) -> object | None:
        """Reopen or issue the one typed application drain from runner failure."""

        process_start_token = error.process_start_token
        launch_evidence = error.launch_evidence
        drain_evidence = error.drain_evidence
        process_drain_sha256 = error.process_drain_sha256
        if drain_evidence is None:
            if process_drain_sha256 is not None:
                raise ProjectAuthorityError(
                    "exceptional process-drain digest lacks backend evidence"
                )
            return None
        if (
            type(process_start_token) is not QuPathProcessStartToken
            or type(launch_evidence)
            not in (
                LinuxSystemdScopeAttachmentV1,
                LinuxLocalProcessGroupAttachmentV1,
                WindowsKillOnCloseJobAttachmentV1,
            )
            or type(drain_evidence)
            not in (
                LinuxSystemdScopeDrainV1,
                LinuxLocalProcessGroupDrainV1,
                WindowsKillOnCloseJobDrainV1,
            )
            or launch_evidence.provider != drain_evidence.provider
            or request.launch_nonce_sha256 is None
        ):
            raise ProjectAuthorityError(
                "exceptional process drain lacks exact typed runtime evidence"
            )
        if type(launch_evidence) is LinuxSystemdScopeAttachmentV1:
            if type(
                drain_evidence
            ) is not LinuxSystemdScopeDrainV1 or not lease._provider._linux_drain_closes_launch_attachment(
                launch_evidence,
                drain_evidence,
            ):
                raise ProjectAuthorityError("exceptional Linux drain differs from its attachment")
        elif type(launch_evidence) is LinuxLocalProcessGroupAttachmentV1:
            if (
                type(drain_evidence) is not LinuxLocalProcessGroupDrainV1
                or launch_evidence.leader_process_id != drain_evidence.leader_process_id
                or launch_evidence.leader_start_epoch_millis
                != drain_evidence.leader_start_epoch_millis
                or launch_evidence.process_group_id != drain_evidence.process_group_id
            ):
                raise ProjectAuthorityError(
                    "exceptional local Linux drain differs from its attachment"
                )
        elif (
            type(drain_evidence) is not WindowsKillOnCloseJobDrainV1
            or launch_evidence.job_name != drain_evidence.job_name
        ):
            raise ProjectAuthorityError("exceptional Windows drain differs from its Job attachment")
        active = lease._provider._validate_live_lease_authority(
            lease,
            require_project=False,
            validate_project_target=False,
        )
        inventory = lease._provider_inventory
        if inventory is None:
            raise ProjectAuthorityError("exceptional process drain lacks runtime inventory")
        process_launch = lease._provider._persisted_runtime_process_launch_for_nonce(
            active,
            inventory,
            launch_nonce_sha256=request.launch_nonce_sha256,
        )
        if process_drain_sha256 is not None:
            digest = _sha(process_drain_sha256, "process_drain_sha256")
            reopened = lease._provider.load_process_drain_evidence(
                lease.designation.identity.project_identity_sha256,
                digest,
            )
            if (
                reopened.process_drain_sha256 != digest
                or reopened.project_identity_sha256
                != lease.designation.identity.project_identity_sha256
                or reopened.acquisition_record_sha256 != lease.acquisition_record_sha256
                or reopened.acquisition_session_challenge_sha256
                != lease.acquisition_session_challenge_sha256
                or reopened.mutation_request_sha256 != lease.mutation_request_sha256
                or reopened.fencing_token_sha256 != lease.fencing_token_sha256
                or reopened.process_start_token != process_start_token
                or reopened.launch_nonce_sha256 != request.launch_nonce_sha256
                or reopened.drain_evidence != drain_evidence
                or reopened.containment_drained is not True
                or (
                    reopened.process_launch_sha256 is None
                    and (
                        process_launch is not None
                        or drain_evidence.termination_reason != "launch_publication_failed"
                    )
                )
                or (
                    reopened.process_launch_sha256 is not None
                    and (
                        process_launch is None
                        or reopened.process_launch_sha256 != process_launch.process_launch_sha256
                        or process_launch.process_start_token != process_start_token
                        or process_launch.launch_evidence != launch_evidence
                    )
                )
            ):
                raise ProjectAuthorityError(
                    "exceptional process drain differs from persisted authority"
                )
            return reopened
        if process_launch is None:
            if drain_evidence.termination_reason != "launch_publication_failed":
                raise ProjectAuthorityError(
                    "exceptional process drain lacks its launch predecessor"
                )
            process_launch_sha256 = None
        else:
            if (
                process_launch.process_start_token != process_start_token
                or process_launch.launch_evidence != launch_evidence
            ):
                raise ProjectAuthorityError(
                    "exceptional process launch differs from persisted authority"
                )
            process_launch_sha256 = process_launch.process_launch_sha256
        terminal_status_sha256 = hashlib.sha256(
            _canonical_bytes(
                {
                    "process_start_token": process_start_token.to_mapping(),
                    "termination_reason": drain_evidence.termination_reason,
                    "exceptional_process_drain": True,
                }
            )
        ).hexdigest()
        issued = lease.record_process_drain(
            process_start_token,
            launch_nonce_sha256=request.launch_nonce_sha256,
            terminal_status_sha256=terminal_status_sha256,
            drain_evidence=drain_evidence,
            process_launch_sha256=process_launch_sha256,
        )
        reopened = lease._provider.load_process_drain_evidence(
            lease.designation.identity.project_identity_sha256,
            issued.process_drain_sha256,
        )
        if reopened != issued:
            raise ProjectAuthorityError("exceptional process drain changed after publication")
        return reopened

    @staticmethod
    def _validate_evidence_payload(
        project_path: Path,
        request: QuPathCatalogueRequestEnvelope,
        evidence: QuPathCatalogueEvidenceEnvelope,
    ) -> None:
        snapshot = evidence.post_save_project_snapshot
        if (
            evidence.operation != request.operation
            or evidence.request_envelope_sha256 != request.request_envelope_sha256
            or evidence.launch_nonce_sha256 != request.launch_nonce_sha256
            or snapshot.project_path.resolve(strict=False) != project_path
            or snapshot.project_uri != canonical_project_uri(project_path)
            or not project_path.is_file()
            or snapshot.qpproj_sha256 != _file_sha256(project_path)
        ):
            raise ProjectAuthorityError("QuPath evidence disagrees with request or project bytes")
        if request.operation == "upsert_entry":
            entry = evidence.entry
            payload = request.payload
            if (
                entry is None
                or entry not in snapshot.entries
                or payload.source_uri not in entry.source_uris
                or any(
                    entry.metadata.get(key) != value
                    for key, value in payload.entry_metadata.items()
                )
                or (
                    payload.expected_project_entry_id is not None
                    and ((payload.entry_mode == "reuse" and entry.project_entry_id != payload.expected_project_entry_id)
                         or (payload.entry_mode == "replace" and entry.project_entry_id == payload.expected_project_entry_id))
                )
            ):
                raise ProjectAuthorityError("QuPath entry evidence disagrees with its upsert")

    @classmethod
    def _validate_abnormal_evidence(
        cls,
        project_path: Path,
        request: QuPathCatalogueRequestEnvelope,
        evidence: QuPathCatalogueEvidenceEnvelope,
        *,
        error: QuPathExecutionError,
        process_drain: object | None,
        lease: ProjectMutationLease,
    ) -> None:
        cls._validate_evidence_payload(project_path, request, evidence)
        reason = getattr(error.drain_evidence, "termination_reason", None)
        if (
            not isinstance(lease, ProjectMutationLease)
            or not isinstance(process_drain, ProjectMutationProcessDrainEvidence)
            or evidence.process_start_token != error.process_start_token
            or reason not in {"nonzero_exit", "timeout", "cancelled"}
            or (
                error.process_drain_sha256 is not None
                and process_drain.process_drain_sha256 != error.process_drain_sha256
            )
            or process_drain.process_start_token != error.process_start_token
            or process_drain.launch_nonce_sha256 != request.launch_nonce_sha256
            or process_drain.drain_evidence != error.drain_evidence
            or process_drain.containment_drained is not True
            or (
                reason == "nonzero_exit"
                and (error.record is None or error.record.exit_status in {None, 0})
            )
        ):
            raise ProjectAuthorityError("abnormal QuPath evidence lacks exact runtime binding")

    @staticmethod
    def _validate_evidence(
        project_path: Path,
        request: QuPathCatalogueRequestEnvelope,
        evidence: QuPathCatalogueEvidenceEnvelope,
        *,
        run: object,
        lease: ProjectMutationLease | None,
    ) -> None:
        QuPathRunnerProjectOperations._validate_evidence_payload(project_path, request, evidence)
        snapshot = evidence.post_save_project_snapshot
        if request.operation != "inspect_project":
            if (
                not isinstance(lease, ProjectMutationLease)
                or type(run) is not QuPathEvidenceRun
                or snapshot.identity_record is None
                or snapshot.identity_record.project_identity_sha256
                != request.project_identity_sha256
                or run.launch_nonce_sha256 != request.launch_nonce_sha256
                or run.process_start_token != evidence.process_start_token
                or type(run.launch_evidence)
                not in (
                    LinuxSystemdScopeAttachmentV1,
                    LinuxLocalProcessGroupAttachmentV1,
                    WindowsKillOnCloseJobAttachmentV1,
                )
                or type(run.drain_evidence)
                not in (
                    LinuxSystemdScopeDrainV1,
                    LinuxLocalProcessGroupDrainV1,
                    WindowsKillOnCloseJobDrainV1,
                )
                or run.launch_evidence.provider != run.drain_evidence.provider
                or not isinstance(run.process_drain_sha256, str)
                or _SHA256.fullmatch(run.process_drain_sha256) is None
            ):
                raise ProjectAuthorityError("QuPath mutation evidence lacks exact runtime binding")
            process_drain = lease._provider.load_process_drain_evidence(
                lease.designation.identity.project_identity_sha256,
                run.process_drain_sha256,
            )
            if (
                process_drain.process_start_token != run.process_start_token
                or process_drain.launch_nonce_sha256 != request.launch_nonce_sha256
                or process_drain.drain_evidence != run.drain_evidence
                or not lease._provider._is_success_process_drain(lease, process_drain)
                or (
                    type(run.launch_evidence) is WindowsKillOnCloseJobAttachmentV1
                    and (
                        type(run.drain_evidence) is not WindowsKillOnCloseJobDrainV1
                        or run.launch_evidence.job_name != run.drain_evidence.job_name
                    )
                )
                or (
                    type(run.launch_evidence) is LinuxSystemdScopeAttachmentV1
                    and (
                        type(run.drain_evidence) is not LinuxSystemdScopeDrainV1
                        or run.launch_evidence.attachment_receipt_sha256
                        != run.drain_evidence.attachment_receipt_sha256
                    )
                )
                or (
                    type(run.launch_evidence) is LinuxLocalProcessGroupAttachmentV1
                    and (
                        type(run.drain_evidence) is not LinuxLocalProcessGroupDrainV1
                        or run.launch_evidence.leader_process_id
                        != run.drain_evidence.leader_process_id
                        or run.launch_evidence.leader_start_epoch_millis
                        != run.drain_evidence.leader_start_epoch_millis
                        or run.launch_evidence.process_group_id
                        != run.drain_evidence.process_group_id
                    )
                )
            ):
                raise ProjectAuthorityError(
                    "QuPath mutation evidence lacks exact persisted runtime binding"
                )


__all__ = ["QuPathRunnerProjectOperations"]
