"""Production orchestration adapter for exact-entry Warpy registration.

Warpy owns transform loading and geometry application inside QuPath. Python constructs
requests, validates immutable records, and applies QC policy; it never interprets or
reimplements a Warpy ``RealTransform``."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from mif_pipeline.qupath_runtime.registration_transform import transform_reader_request

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname
import uuid

from mif_pipeline.io.backends.base import image_identity_member
from mif_pipeline.pipeline_core.config import AnalysisConfig
from mif_pipeline.pipeline_core.interactions import resolve_resume_envelope
from mif_pipeline.pipeline_core.models import ROIAnalysisError
from mif_pipeline.pipeline_core.runtime_lifecycle import (
    RuntimeLifecycleBorrower,
    RuntimeLifecycleError,
    RuntimeLifecycleOwner,
    RuntimeTeardownPermit,
)
from mif_pipeline.pipeline_core.transfer_review_authority import (
    TransferReviewAuthorityError,
    TransferReviewResponseResolver,
    authenticate_transfer_review_response_resolver,
)
from mif_pipeline.pipeline_core.schema_registry import (
    assert_current_writer,
)
from mif_pipeline.pipeline_core.process_containment import (
    retain_authoritative_lock_descriptor,
)
from mif_pipeline.pipeline_core.stage_result import InteractionReason, StageResult
from mif_pipeline.qupath_runtime.project import ProjectLayout, prepare_project
from mif_pipeline.qupath_runtime.registration_session import (
    ExistingRegistrationProjectSession,
    RegistrationProjectEntryObservation,
)
from mif_pipeline.qupath_runtime.runner import QuPathRegistrationProcessCompletion, QuPathRunner
from mif_pipeline.tissue_detection.generations import GenerationStatus, TissueSectionGeneration

from .backend import (
    REGISTRATION_QC_SCHEMA_VERSION,
    REGISTRATION_SCHEMA_VERSION,
    PhysicalCalibration,
    RegistrationImportScope,
    RegistrationQCEvidence,
    RegistrationQCPolicy,
    RegistrationQCRecord,
    RegistrationSamplingContext,
    RegistrationRecord,
    SectionRegistrationQCRecord,
    TransferQCPolicy,
    TransferQCRecord,
    TransferAttemptRecord,
    canonical_transfer_attempt_id,
    import_registration_record,
    import_transfer_qc_records,
    registration_record_sha256,
    resolve_registration_review,
    transfer_attempt_sha256,
)
from .contained_mutation_journal import (
    WarpyContainedMutationJournal,
    diagnose_warpy_containment_lease,
    validate_warpy_containment_record,
)
from .secure_store import (
    FileIdentity,
    PosixFileIdentity,
    UnsupportedSecureStoreBackend,
    WindowsFileIdentity,
    WindowsSecureStoreBackend,
    file_identity_from_mapping,
    secure_store_backend,
)
from .transfer_review import (
    TransferReviewDecision,
    TransferReviewProposal,
    build_transfer_review_proposal,
    issue_transfer_review_decision,
    transfer_review_proposal_sha256,
)

# ==============================================================================
#                                    Executor
# ==============================================================================


def _add_bounded_diagnostic_note(
    primary: BaseException,
    prefix: str,
    secondary: BaseException,
) -> None:
    """Best-effort secondary diagnostics must never replace ``primary``."""

    try:
        try:
            rendered = repr(secondary)
        except BaseException:
            try:
                rendered = str(secondary)
            except BaseException:
                rendered = "<unrenderable secondary exception>"
        note = (prefix + rendered)[:512]
        try:
            primary.add_note(note)
        except BaseException:
            pass
    except BaseException:
        pass


@dataclass(frozen=True, slots=True)
class _TransferBatchResult:
    """Every immutable transfer decision plus live authoritative commit evidence."""

    records: tuple[TransferQCRecord, ...]
    commit_evidence: Mapping[str, Any] | None
    attempt: TransferAttemptRecord
    commit_outcomes: tuple[Mapping[str, Any], ...]


class _RetainedPosixLockAuthority:
    """Keep one lock inode alive and bind its name to the open description."""

    def __init__(self, path: Path, descriptor: int, identity: tuple[int, int]) -> None:
        self.path = path
        self.descriptor = descriptor
        self.identity = identity
        self._thread_lock = threading.RLock()
        self._closed = False

    def validate(self) -> None:
        if self._closed:
            raise ROIAnalysisError("retained Warpy lock authority is closed")
        with _open_directory_no_follow(self.path.parent, create=False) as (
            directory_fd,
            _identity,
        ):
            named = _secure_child_metadata(directory_fd, self.path.name)
            if (
                named is None
                or _regular_file_identity(named) != self.identity
                or _regular_file_identity(os.fstat(self.descriptor)) != self.identity
            ):
                raise ROIAnalysisError("Warpy transfer lock identity changed")

    @contextmanager
    def locked(self):
        with self._thread_lock:
            self.validate()
            with _exclusive_posix_file_lock(self.descriptor):
                with retain_authoritative_lock_descriptor(self.descriptor):
                    self.validate()
                    try:
                        yield
                    finally:
                        self.validate()

    def close(self) -> None:
        with self._thread_lock:
            if not self._closed:
                self._closed = True
                os.close(self.descriptor)


@dataclass(frozen=True, slots=True)
class _RetainedTransferAuthorityScope:
    project_file: Path
    attempt_id: str
    operation_lock_identity: tuple[int, int]


class _RetainedTransferLockAuthorities:
    def __init__(
        self,
        scope: _RetainedTransferAuthorityScope,
        operation: _RetainedPosixLockAuthority,
    ) -> None:
        self.scope = scope
        self.operation = operation
        self.hierarchies: dict[Path, _RetainedPosixLockAuthority] = {}

    def validate_scope(
        self,
        *,
        project_file: Path,
        attempt_id: str,
        operation_lock_identity: FileIdentity | tuple[int, int],
    ) -> None:
        expected = _as_posix_tuple(operation_lock_identity, "transfer operation lock identity")
        if self.scope != _RetainedTransferAuthorityScope(
            project_file=project_file,
            attempt_id=attempt_id,
            operation_lock_identity=expected,
        ):
            raise ROIAnalysisError("retained Warpy authority scope changed")

    def close(self) -> None:
        failures: list[BaseException] = []
        authorities = tuple(self.hierarchies.values())
        self.hierarchies.clear()
        for authority in authorities:
            try:
                authority.close()
            except BaseException as exc:
                failures.append(exc)
        try:
            self.operation.close()
        except BaseException as exc:
            failures.append(exc)
        if failures:
            primary = failures[0]
            for failure in failures[1:]:
                _add_bounded_diagnostic_note(
                    primary,
                    "additional retained Warpy authority close failure: ",
                    failure,
                )
            raise primary


@dataclass(frozen=True, slots=True)
class _TransferAttemptJournal:
    """Validated durable lifecycle for one exact canonical transfer attempt."""

    attempt: TransferAttemptRecord
    project_file: Path
    attempt_path: Path
    events_dir: Path
    reused: bool
    entries: tuple[Mapping[str, Any], ...]
    outcomes: tuple[Mapping[str, Any], ...]
    commit_evidence: Mapping[str, Any] | None
    cleanup_evidence: Mapping[str, Any] | None
    committed_entry_sequence: int | None
    cleanup_entry_sequence: int | None
    active_lease: Mapping[str, Any] | None
    active_lease_entry_sequence: int | None
    last_lease_epoch: int
    containment_diagnostic_lease: Mapping[str, Any] | None
    store_identity: FileIdentity | tuple[int, int]
    events_identity: FileIdentity | tuple[int, int]
    operation_lock_identity: FileIdentity | tuple[int, int]
    retained_lock_authorities: _RetainedTransferLockAuthorities | None = None


class _TransferCommitFailure(Exception):
    def __init__(
        self,
        attempt: TransferAttemptRecord,
        failure: Exception,
        *,
        cleanup_required: bool,
        journal: _TransferAttemptJournal,
    ) -> None:
        super().__init__(str(failure))
        self.attempt = attempt
        self.failure = failure
        self.cleanup_required = cleanup_required
        self.journal = journal


class _CommitBoundaryFailure(Exception):
    def __init__(self, failure: Exception, *, cleanup_required: bool) -> None:
        super().__init__(str(failure))
        self.failure = failure
        self.cleanup_required = cleanup_required


class _LeaseReleaseFailure(Exception):
    def __init__(self, journal: _TransferAttemptJournal, failure: Exception) -> None:
        super().__init__(str(failure))
        self.journal = journal
        self.failure = failure


class _OperationLockAuthorityFailure(ROIAnalysisError):
    def __init__(self, journal: _TransferAttemptJournal, message: str) -> None:
        super().__init__(message)
        self.journal = journal


class _WarpyContainmentManualReviewRequired(ROIAnalysisError):
    def __init__(self, journal: _TransferAttemptJournal) -> None:
        super().__init__("existing Warpy containment evidence requires manual review")
        self.journal = journal


class ProductionRegistrationExecutor:
    """Import and review Warpy transforms for actual QuPath project entries."""

    def __init__(
        self,
        *,
        runner: object,
        project_preparer=prepare_project,
        runtime_lifecycle: RuntimeLifecycleBorrower | None = None,
        transfer_review_response_resolver: TransferReviewResponseResolver | None = None,
        allow_object_transfer: bool = True,
    ) -> None:
        if type(allow_object_transfer) is not bool:
            raise ROIAnalysisError("allow_object_transfer must be a bool")
        self._allow_object_transfer = allow_object_transfer
        self._runner = runner
        self._project_preparer = project_preparer
        if transfer_review_response_resolver is None:
            self._transfer_review_response_resolver = None
        else:
            try:
                self._transfer_review_response_resolver = (
                    authenticate_transfer_review_response_resolver(
                        transfer_review_response_resolver
                    )
                )
            except TransferReviewAuthorityError as exc:
                raise ROIAnalysisError(
                    "transfer review resolver is not state-owner-issued"
                ) from exc
        if runtime_lifecycle is not None and not isinstance(
            runtime_lifecycle, RuntimeLifecycleBorrower
        ):
            raise ROIAnalysisError("registration runtime lifecycle is invalid")
        self._runtime_owner = RuntimeLifecycleOwner.open() if runtime_lifecycle is None else None
        self._runtime_lifecycle = (
            self._runtime_owner.borrower if self._runtime_owner is not None else runtime_lifecycle
        )
        assert self._runtime_lifecycle is not None
        self._warpy_lock_authorities: dict[
            _RetainedTransferAuthorityScope, _RetainedTransferLockAuthorities
        ] = {}
        self._warpy_lock_authorities_guard = threading.RLock()
        self._warpy_lifetime = threading.Condition(self._warpy_lock_authorities_guard)
        self._active_transfer_calls = 0
        self._closed = False
        self._close_complete = False
        self._close_failure: BaseException | None = None
        self._physical_close_owner: threading.Thread | None = None

    def __enter__(self) -> "ProductionRegistrationExecutor":
        self._require_open()
        return self

    def __exit__(self, _type, value, _traceback) -> None:
        try:
            self.close()
        except BaseException as close_exc:
            if value is None:
                raise
            _add_bounded_diagnostic_note(
                value,
                "registration executor close also failed: ",
                close_exc,
            )

    def __del__(self) -> None:
        if getattr(self, "_runtime_owner", None) is None:
            return
        try:
            self.close()
        except BaseException:
            pass

    def _require_open(self) -> None:
        with self._warpy_lifetime:
            if self._closed:
                raise ROIAnalysisError("registration executor is closed")

    @property
    def is_quiescent(self) -> bool:
        return self._runtime_lifecycle.is_kind_quiescent("registration-mutation")

    @contextmanager
    def _admitted_transfer(self):
        try:
            runtime_token = self._runtime_lifecycle.acquire("registration-mutation")
        except RuntimeLifecycleError as exc:
            raise ROIAnalysisError("registration runtime is not open") from exc
        try:
            with self._warpy_lifetime:
                if self._closed:
                    raise ROIAnalysisError("registration executor is closed")
                self._active_transfer_calls += 1
            try:
                yield
            finally:
                with self._warpy_lifetime:
                    self._active_transfer_calls -= 1
                    self._warpy_lifetime.notify_all()
        finally:
            runtime_token.release()

    def close(self, *, teardown_permit: RuntimeTeardownPermit | None = None) -> None:
        caller = threading.current_thread()
        with self._warpy_lifetime:
            if self._closed:
                if not self._close_complete and self._physical_close_owner is caller:
                    return
                while not self._close_complete:
                    self._warpy_lifetime.wait()
                if self._close_failure is not None:
                    raise self._close_failure
                return
            if self._active_transfer_calls:
                raise ROIAnalysisError("registration executor has an active transfer")
            owner = self._runtime_owner
            if owner is None:
                if teardown_permit is None:
                    raise ROIAnalysisError(
                        "borrowed registration executor requires teardown authority"
                    )
                try:
                    self._runtime_lifecycle.authenticate_teardown(teardown_permit)
                except RuntimeLifecycleError as exc:
                    raise ROIAnalysisError("registration teardown authority is invalid") from exc
                permit = teardown_permit
            else:
                if teardown_permit is not None:
                    raise ROIAnalysisError(
                        "standalone registration executor rejects foreign teardown authority"
                    )
                try:
                    permit = owner.begin_teardown()
                except RuntimeLifecycleError as exc:
                    raise ROIAnalysisError("registration executor has live runtime uses") from exc
            self._closed = True
            self._physical_close_owner = caller
            authorities = tuple(dict.fromkeys(self._warpy_lock_authorities.values()))
            self._warpy_lock_authorities.clear()
        failures: list[BaseException] = []
        primary: BaseException | None = None
        try:
            for authority in authorities:
                try:
                    authority.close()
                except BaseException as exc:
                    failures.append(exc)
            if failures:
                primary = failures[0]
                for failure in failures[1:]:
                    _add_bounded_diagnostic_note(
                        primary,
                        "additional Warpy executor close failure: ",
                        failure,
                    )
        finally:
            with self._warpy_lifetime:
                self._close_failure = primary
                self._physical_close_owner = None
                self._close_complete = True
                self._warpy_lifetime.notify_all()
            if owner is not None:
                owner.finish_teardown(permit, failure=primary)
        if primary is not None:
            raise primary

    def _retained_warpy_authority(
        self, project: object, attempt: TransferAttemptRecord
    ) -> _RetainedTransferLockAuthorities | None:
        project_file = _absolute_without_links(getattr(project, "project_file"))
        with self._warpy_lock_authorities_guard:
            candidates = tuple(
                authority
                for scope, authority in self._warpy_lock_authorities.items()
                if scope.project_file == project_file and scope.attempt_id == attempt.attempt_id
            )
        if len(candidates) > 1:
            raise ROIAnalysisError("retained Warpy authority identity forked")
        return None if not candidates else candidates[0]

    def _remember_warpy_authority(
        self, journal: _TransferAttemptJournal
    ) -> _TransferAttemptJournal:
        authority = journal.retained_lock_authorities
        if authority is None:
            return journal
        try:
            authority.validate_scope(
                project_file=journal.project_file,
                attempt_id=journal.attempt.attempt_id,
                operation_lock_identity=journal.operation_lock_identity,
            )
        except BaseException as exc:
            self._close_rejected_warpy_authority(authority, exc)
            raise
        with self._warpy_lifetime:
            if self._closed:
                failure = ROIAnalysisError("registration executor is closed")
                self._close_rejected_warpy_authority(authority, failure)
                raise failure
            prior = self._warpy_lock_authorities.get(authority.scope)
            if prior is authority:
                return journal
            if prior is None:
                self._warpy_lock_authorities[authority.scope] = authority
                return journal
        authority.close()
        prior.validate_scope(
            project_file=journal.project_file,
            attempt_id=journal.attempt.attempt_id,
            operation_lock_identity=journal.operation_lock_identity,
        )
        return replace(journal, retained_lock_authorities=prior)

    @staticmethod
    def _close_rejected_warpy_authority(
        authority: _RetainedTransferLockAuthorities,
        primary: BaseException,
    ) -> None:
        try:
            authority.close()
        except BaseException as close_exc:
            _add_bounded_diagnostic_note(
                primary,
                "rejected Warpy authority cleanup also failed: ",
                close_exc,
            )

    def _release_terminal_warpy_authority(self, journal: _TransferAttemptJournal) -> None:
        authority = journal.retained_lock_authorities
        if authority is None:
            return
        if (
            journal.active_lease is not None
            or not journal.outcomes
            or (journal.outcomes[-1]["status"] not in {"not_required", "quarantined", "aborted"})
        ):
            return
        with self._warpy_lock_authorities_guard:
            if self._warpy_lock_authorities.get(authority.scope) is authority:
                del self._warpy_lock_authorities[authority.scope]
        authority.close()

    def __call__(self, invocation: object) -> StageResult:
        if not self._allow_object_transfer and getattr(invocation, "action", None) in {
            "accept_registration_and_transfer",
            "accept_transfers",
            "reject_transfers",
            "retry_transfer_commit",
            "quarantine_transfer_attempt",
            "abort_transfer_attempt",
        }:
            raise ROIAnalysisError("object transfer is unavailable in registration-only mode")
        with self._admitted_transfer():
            return self._call_admitted(invocation)

    def execute_existing_project(
        self,
        invocation: object,
        *,
        project: ProjectLayout,
        session: ExistingRegistrationProjectSession,
        recovered_completion: QuPathRegistrationProcessCompletion | None = None,
    ) -> tuple[StageResult, QuPathRegistrationProcessCompletion]:
        """Join a retained exact-project import to the shared scientific owner.

        Invalid admission inputs reject before launch. Once the runner returns,
        its completion is retained separately even when scientific QC rejects.
        """
        if self._allow_object_transfer is not False:
            raise ROIAnalysisError(
                "existing-project execution requires a registration-only executor"
            )
        config = getattr(invocation, "config", None)
        state = getattr(invocation, "state", None)
        if getattr(invocation, "action", None) not in {
            None,
            "import_warpy_transform",
            "accept_registration",
            "reject",
        }:
            raise ROIAnalysisError("existing-project registration does not allow this action")
        if (
            type(self._runner) is not QuPathRunner
            or type(project) is not ProjectLayout
            or type(session) is not ExistingRegistrationProjectSession
            or not isinstance(config, AnalysisConfig)
            or not isinstance(state, Mapping)
            or config.registration.mode != "warpy"
        ):
            raise ROIAnalysisError(
                "existing-project registration requires exact runtime and Warpy inputs"
            )
        with self._admitted_transfer():
            generation = _accepted_generation(state)
            snapshot = session.current_project_projection()
            if snapshot.moving_entry.source_image_sha256 == generation.canonical_image_sha256:
                raise ROIAnalysisError("moving and canonical mIF image scopes must differ")
            registration = config.registration
            if (
                snapshot.project_uri != project.project_file.resolve().as_uri()
                or snapshot.fixed_entry.source_uri.rstrip("/") != config.input_image.resolve().as_uri()
                or registration.moving_image is None
                or snapshot.moving_entry.source_uri.rstrip("/") != registration.moving_image.resolve().as_uri()
                or not registration.moving_image_id
                or snapshot.fixed_entry.source_image_sha256 != generation.canonical_image_sha256
                or snapshot.transform.presence != "present"
            ):
                raise ROIAnalysisError(
                    "existing registration session differs from the configured image scope"
                )
            transform_file = Path(url2pathname(urlsplit(snapshot.transform.transform_uri).path))
            if (
                registration.transform_file is not None
                and registration.transform_file.resolve() != transform_file
            ):
                raise ROIAnalysisError(
                    "configured transform differs from the existing registration session"
                )

            def entry(
                observed: RegistrationProjectEntryObservation, image_id: str
            ) -> dict[str, Any]:
                return {
                    "project_file": str(project.project_file.resolve()),
                    "project_entry_id": observed.project_entry_id,
                    "project_entry_path": url2pathname(urlsplit(observed.entry_root_uri).path),
                    "image_path": str(Path(url2pathname(urlsplit(observed.source_uri).path))),
                    "image_id": image_id,
                    "image_sha256": observed.source_image_sha256,
                    "physical_calibration": {
                        "x_um_per_px": float.fromhex(observed.x_um_per_px_hex),
                        "y_um_per_px": float.fromhex(observed.y_um_per_px_hex),
                    },
                }

            fixed = entry(snapshot.fixed_entry, generation.canonical_image_id)
            moving = entry(snapshot.moving_entry, registration.moving_image_id)
            moving.update(
                moving_modality=registration.moving_modality,
                source_metadata=_mutable_json(registration.moving_image_metadata),
            )
            expected_hashes = (
                snapshot.moving_entry.source_image_sha256,
                snapshot.fixed_entry.source_image_sha256,
            )
            before_hashes = (
                _file_sha256(image_identity_member(Path(moving["image_path"]))),
                _file_sha256(image_identity_member(Path(fixed["image_path"]))),
            )
            if before_hashes != expected_hashes:
                raise ROIAnalysisError("registration input images differ from the retained session")
            request = _registration_import_request(
                invocation,
                generation,
                fixed,
                moving,
                expected_hashes[0],
                transform_file,
            )
            if request["transform_file_sha256"] != snapshot.transform.content_sha256:
                raise ROIAnalysisError("registration transform differs from the retained session")
            if recovered_completion is None:
                issued = session.issue_registration_import_request(
                    registration_inputs={
                        key: request[key]
                        for key in (
                            "registration_id",
                            "moving_image_id",
                            "fixed_image_id",
                            "coordinate_pre_transform",
                            "coordinate_post_transform",
                            "accepted_tissue_sections",
                            "sample_spacing_px",
                        )
                    }
                )
                if issued.script_request != request:
                    raise ROIAnalysisError(
                        "provider-issued registration request differs from the scientific owner"
                    )
                authority = self._runner.issue_registration_process_authority(
                    session=session, request=issued
                )
                completion = self._runner.run_registration_process(authority)
            else:
                completion = recovered_completion
            if type(completion) is not QuPathRegistrationProcessCompletion:
                raise ROIAnalysisError("registration runner returned an invalid completion")
            if recovered_completion is not None:
                issued = completion.request
            try:
                after_hashes = (
                    _file_sha256(image_identity_member(Path(moving["image_path"]))),
                    _file_sha256(image_identity_member(Path(fixed["image_path"]))),
                )
                if before_hashes != after_hashes or after_hashes != expected_hashes:
                    raise ROIAnalysisError(
                        "registration input image scope changed during invocation"
                    )
                if (
                    _file_sha256(transform_file) != snapshot.transform.content_sha256
                    or session.current_project_projection() != snapshot
                ):
                    raise ROIAnalysisError(
                        "existing registration project scope changed during invocation"
                    )
                if completion.request is not issued or completion.script_request != request:
                    raise ROIAnalysisError(
                        "registration completion differs from the exact issued request"
                    )
                if completion.disposition != "completed":
                    return (
                        _failed(
                            config,
                            generation,
                            "warpy_registration_process_failed",
                            "QuPath could not finish image alignment. Check the error details before trying again.",
                        ),
                        completion,
                    )
                result = self._evaluate_registration_evidence(
                    invocation,
                    config,
                    generation,
                    project,
                    fixed,
                    moving,
                    request,
                    completion.evidence,
                )
            except (OSError, ROIAnalysisError, TypeError, ValueError) as exc:
                result = _failed(
                    config,
                    generation,
                    "warpy_registration_invalid",
                    str(exc),
                    exception_type=type(exc).__name__,
                )
            return result, completion

    def _call_admitted(self, invocation: object) -> StageResult:
        config = getattr(invocation, "config", None)
        state = getattr(invocation, "state", None)
        if not isinstance(config, AnalysisConfig) or not isinstance(state, Mapping):
            raise ROIAnalysisError("registration executor requires a workflow invocation")
        try:
            generation = _accepted_generation(state)
        except ROIAnalysisError as exc:
            return _failed(config, None, "accepted_tissue_generation_invalid", str(exc))
        (
            mode,
            moving_image,
            moving_image_id,
            transform_file,
            moving_modality,
            source_metadata,
        ) = _effective_registration(config, state)
        if mode == "prompt":
            return _failed(
                config,
                generation,
                "registration_choice_unresolved",
                "moving-reference availability and use must be resolved before registration",
            )
        if mode in {"none", "skip"}:
            fallback = _fallback_audit(state)
            return StageResult.completed(
                stage="registration",
                slide_id=config.slide_id,
                generation_id=generation.generation_id,
                affected_object_ids=tuple(item.section_id for item in generation.sections),
                outputs={
                    "mode": mode,
                    "registration": None,
                    "moving_reference": None,
                    **fallback,
                },
            )
        if moving_image is None or moving_image_id is None:
            return _failed(
                config,
                generation,
                "registration_moving_image_required",
                f"{mode} mode requires a local moving image and exact image ID",
            )
        if not moving_image.is_file():
            return _failed(
                config,
                generation,
                "registration_moving_image_missing",
                f"moving image does not exist or is not a file: {moving_image}",
            )
        moving_sha256 = _file_sha256(image_identity_member(moving_image))
        if moving_sha256 == generation.canonical_image_sha256:
            return _failed(
                config,
                generation,
                "registration_images_not_distinct",
                "moving and canonical mIF image scopes must differ",
            )
        try:
            project = self._project_preparer(config)
            fixed = self._canonical_evidence(project)
            if Path(str(fixed["image_path"])).resolve() != config.input_image.resolve():
                raise ROIAnalysisError(
                    "canonical project entry does not reference the configured mIF image"
                )
            if _file_sha256(image_identity_member(config.input_image)) != generation.canonical_image_sha256:
                raise ROIAnalysisError(
                    "canonical project-entry evidence conflicts with exact image identity"
                )
            fixed = {
                **fixed,
                "image_id": generation.canonical_image_id,
                "image_sha256": generation.canonical_image_sha256,
            }
            moving = self._add_moving_image(
                project,
                moving_image,
                moving_image_id,
                moving_sha256,
                moving_modality,
                source_metadata,
            )
            if moving["project_file"] != fixed["project_file"]:
                raise ROIAnalysisError(
                    "moving and fixed entries do not belong to one exact project"
                )
        except Exception as exc:
            return _failed(
                config,
                generation,
                "qupath_project_entry_failed",
                str(exc),
                exception_type=type(exc).__name__,
            )
        if mode == "side_by_side":
            fallback = _fallback_audit(state)
            return StageResult.completed(
                stage="registration",
                slide_id=config.slide_id,
                generation_id=generation.generation_id,
                affected_object_ids=tuple(item.section_id for item in generation.sections),
                outputs={
                    "mode": "side_by_side",
                    "registration": None,
                    "moving_reference": dict(moving),
                    "fixed_reference": dict(fixed),
                    **fallback,
                },
            )
        expected_transform = Path(str(fixed["project_entry_path"])).resolve() / (
            f"transform_{fixed['project_entry_id']}_{moving['project_entry_id']}.json"
        )
        if transform_file is not None and transform_file.resolve() != expected_transform:
            return _failed(
                config,
                generation,
                "warpy_transform_path_invalid",
                "Warpy transform must use the exact fixed-entry "
                "transform_<fixed>_<moving>.json path",
            )
        transform_file = expected_transform
        if not transform_file.is_file():
            return StageResult.needs_interaction(
                stage="registration",
                slide_id=config.slide_id,
                generation_id=generation.generation_id,
                reason=InteractionReason.WARPY_REGISTRATION_REQUIRED,
                affected_object_ids=tuple(item.section_id for item in generation.sections),
                message="Align the paired image in QuPath, save the transform, then continue.",
                allowed_actions=(
                    "import_warpy_transform",
                    "side_by_side",
                    "skip_moving_reference",
                ),
                resume_context={
                    "schema_version": "warpy-registration-interaction/v1",
                    "project_file": fixed["project_file"],
                    "moving_project_entry_id": moving["project_entry_id"],
                    "fixed_project_entry_id": fixed["project_entry_id"],
                    "expected_transform_file": str(transform_file),
                    "moving_image_id": moving["image_id"],
                    "fixed_image_id": fixed["image_id"],
                },
                checkpoint_id="pending-workflow-checkpoint",
            )
        try:
            return self._import_registration(
                invocation,
                config,
                generation,
                project,
                fixed,
                moving,
                moving_sha256,
                transform_file,
            )
        except (OSError, ROIAnalysisError, TypeError, ValueError) as exc:
            return _failed(
                config,
                generation,
                "warpy_registration_invalid",
                str(exc),
                exception_type=type(exc).__name__,
            )

    def _canonical_evidence(self, project: object) -> Mapping[str, Any]:
        method = getattr(self._runner, "canonical_image_evidence", None)
        if not callable(method):
            raise ROIAnalysisError("QuPath runner cannot resolve canonical project-entry evidence")
        return _required_mapping(
            method(project),
            {
                "project_file",
                "project_entry_id",
                "project_entry_path",
                "image_path",
                "physical_calibration",
            },
            "canonical project-entry evidence",
        )

    def _add_moving_image(
        self,
        project: object,
        moving_image: Path,
        moving_image_id: str,
        moving_sha256: str,
        moving_modality: str,
        source_metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        method = getattr(self._runner, "add_moving_image", None)
        if not callable(method):
            raise ROIAnalysisError("QuPath runner cannot add an exact moving project entry")
        evidence = _required_mapping(
            method(
                project,
                moving_image,
                image_id=moving_image_id,
                image_sha256=moving_sha256,
                moving_modality=moving_modality,
                source_metadata=_mutable_json(source_metadata),
            ),
            {
                "project_file",
                "project_entry_id",
                "image_id",
                "image_sha256",
                "image_path",
                "project_entry_path",
                "moving_modality",
                "source_metadata",
                "physical_calibration",
            },
            "moving project-entry evidence",
        )
        expected = {
            "image_id": moving_image_id,
            "image_sha256": moving_sha256,
            "image_path": str(moving_image.resolve()),
            "moving_modality": moving_modality,
            "source_metadata": _mutable_json(source_metadata),
        }
        if any(evidence[name] != value for name, value in expected.items()):
            raise ROIAnalysisError("moving project-entry evidence conflicts with its source scope")
        return evidence

    def _import_registration(
        self,
        invocation: object,
        config: AnalysisConfig,
        generation: TissueSectionGeneration,
        project: object,
        fixed: Mapping[str, Any],
        moving: Mapping[str, Any],
        moving_sha256: str,
        transform_file: Path,
    ) -> StageResult:
        action = getattr(invocation, "action", None)
        action_context = getattr(invocation, "action_context", {})
        if not isinstance(action_context, Mapping):
            raise ROIAnalysisError("registration action context must be a mapping")
        if action in {
            "retry_transfer_commit",
            "quarantine_transfer_attempt",
            "abort_transfer_attempt",
        }:
            try:
                return self._resume_transfer_attempt(
                    action=action,
                    action_context=action_context,
                    config=config,
                    generation=generation,
                    project=project,
                    fixed=fixed,
                    moving=moving,
                )
            except _OperationLockAuthorityFailure as exc:
                decision_reference = _transfer_review_decision_reference_from_context(
                    project,
                    action_context,
                    exc.journal.attempt,
                )
                return _transfer_commit_recovery_result(
                    config,
                    generation,
                    exc.journal.attempt,
                    exc,
                    fixed,
                    moving,
                    journal=exc.journal,
                    cleanup_required=True,
                    decision_reference=decision_reference,
                )
            except _WarpyContainmentManualReviewRequired as exc:
                decision_reference = _transfer_review_decision_reference_from_context(
                    project,
                    action_context,
                    exc.journal.attempt,
                )
                return _warpy_containment_manual_review_result(
                    config,
                    generation,
                    exc.journal,
                    fixed,
                    moving,
                    decision_reference=decision_reference,
                )
        if action in {"accept_transfers", "reject_transfers"}:
            return self._resume_transfer_review(
                action=action,
                action_context=action_context,
                config=config,
                generation=generation,
                project=project,
                fixed=fixed,
                moving=moving,
            )
        request = _registration_import_request(
            invocation,
            generation,
            fixed,
            moving,
            moving_sha256,
            transform_file,
        )
        method = getattr(self._runner, "import_warpy_registration", None)
        if not callable(method):
            raise ROIAnalysisError("QuPath runner cannot import a Warpy transform")
        before_input_hashes = (
            _file_sha256(image_identity_member(Path(str(moving["image_path"])))),
            _file_sha256(image_identity_member(Path(str(fixed["image_path"])))),
        )
        raw_evidence = method(project, request)
        after_input_hashes = (
            _file_sha256(image_identity_member(Path(str(moving["image_path"])))),
            _file_sha256(image_identity_member(Path(str(fixed["image_path"])))),
        )
        if before_input_hashes != after_input_hashes or after_input_hashes != (
            moving_sha256,
            generation.canonical_image_sha256,
        ):
            raise ROIAnalysisError("registration input image scope changed during invocation")
        return self._evaluate_registration_evidence(
            invocation,
            config,
            generation,
            project,
            fixed,
            moving,
            request,
            raw_evidence,
        )

    def _evaluate_registration_evidence(
        self,
        invocation: object,
        config: AnalysisConfig,
        generation: TissueSectionGeneration,
        project: object,
        fixed: Mapping[str, Any],
        moving: Mapping[str, Any],
        request: Mapping[str, Any],
        raw_evidence: object,
    ) -> StageResult:
        """Evaluate completed native evidence through the existing QC/review owner.

        This private evaluator grants no launch or completion authority. Its
        caller retains the native completion and before/after image checks.
        """
        action = getattr(invocation, "action", None)
        action_context = getattr(invocation, "action_context", {})
        if not isinstance(action_context, Mapping):
            raise ROIAnalysisError("registration action context must be a mapping")
        (
            pending_record,
            pending_digest,
            lineage_predecessor,
            lineage_predecessor_digest,
        ) = _pending_registration(action_context)
        if action in {"accept_registration", "accept_registration_and_transfer", "reject"}:
            if pending_record is None or pending_digest is None:
                raise ROIAnalysisError(
                    "registration review action requires the exact checkpointed pending record"
                )
        revision = pending_record.revision if pending_record is not None else 1
        supersedes = (
            pending_record.supersedes_registration_id if pending_record is not None else None
        )
        registration_id = request["registration_id"]
        moving_sha256 = request["moving_image_sha256"]
        transform_file = Path(request["transform_file"])
        transform_sha256 = request["transform_file_sha256"]
        section_scope = request["accepted_tissue_sections"]
        evidence = _required_mapping(
            raw_evidence,
            {
                "registration_id",
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
                "provider",
                "provider_version",
                "runtime_versions",
                "moving_physical_calibration",
                "fixed_physical_calibration",
                "coordinate_pre_transform",
                "coordinate_post_transform",
                "global_evidence",
                "section_evidence",
            },
            "Warpy registration evidence",
        )
        for name in (
            "moving_project_entry_id",
            "fixed_project_entry_id",
            "moving_image_id",
            "fixed_image_id",
            "moving_image_path",
            "fixed_image_path",
            "moving_image_sha256",
            "fixed_image_sha256",
        ):
            if evidence[name] != request[name]:
                raise ROIAnalysisError(f"Warpy registration live image scope conflicts for {name}")
        if (
            evidence["coordinate_pre_transform"] is not None
            or evidence["coordinate_post_transform"] is not None
        ):
            raise ROIAnalysisError(
                "Warpy 0.5.0 full-image registration does not support non-null "
                "coordinate pre/post transforms"
            )
        moving_calibration = PhysicalCalibration.from_mapping(
            evidence["moving_physical_calibration"]
        )
        fixed_calibration = PhysicalCalibration.from_mapping(evidence["fixed_physical_calibration"])
        if moving_calibration != PhysicalCalibration.from_mapping(
            moving["physical_calibration"]
        ) or fixed_calibration != PhysicalCalibration.from_mapping(fixed["physical_calibration"]):
            raise ROIAnalysisError(
                "Warpy registration calibration evidence conflicts with exact project entries"
            )
        policy = RegistrationQCPolicy(**config.registration.registration_qc_policy)
        global_evidence = RegistrationQCEvidence.from_mapping(evidence["global_evidence"])
        global_status, _ = policy.evaluate(global_evidence, check_coverage=False)
        raw_sections = evidence["section_evidence"]
        if isinstance(raw_sections, (str, bytes)) or not isinstance(raw_sections, Sequence):
            raise ROIAnalysisError("Warpy section evidence must be an array")
        section_records: list[SectionRegistrationQCRecord] = []
        for expected, raw in zip(section_scope, raw_sections, strict=True):
            row = _required_mapping(
                raw,
                {
                    "section_id",
                    "tissue_roi_id",
                    "tissue_path_object_id",
                    "pipeline_stage",
                    "generation_status",
                    "review_status",
                    "path_class",
                    "evidence",
                },
                "Warpy section evidence",
            )
            if any(row[name] != expected[name] for name in ("section_id", "tissue_roi_id")):
                raise ROIAnalysisError(
                    "Warpy section evidence scope does not match accepted tissue"
                )
            if row["tissue_path_object_id"] != expected["tissue_path_object_id"]:
                raise ROIAnalysisError(
                    "Warpy section evidence PathObject UUID does not match accepted tissue"
                )
            if any(
                row[name] != expected[name]
                for name in (
                    "pipeline_stage",
                    "generation_status",
                    "review_status",
                )
            ):
                raise ROIAnalysisError("Warpy TissueSection metadata evidence is stale")
            item_evidence = RegistrationQCEvidence.from_mapping(row["evidence"])
            status, _ = policy.evaluate(item_evidence)
            section_records.append(
                SectionRegistrationQCRecord(
                    section_id=row["section_id"],
                    tissue_roi_id=row["tissue_roi_id"],
                    tissue_path_object_id=row["tissue_path_object_id"],
                    pipeline_stage=row["pipeline_stage"],
                    generation_status=row["generation_status"],
                    review_status=row["review_status"],
                    path_class=row["path_class"],
                    evidence=item_evidence,
                    computed_status=status,
                )
            )
        statuses = {global_status, *(item.computed_status for item in section_records)}
        computed_status = (
            "failed"
            if "failed" in statuses
            else ("uncertain" if "uncertain" in statuses else "passed")
        )
        envelope = resolve_resume_envelope(action_context)
        response = envelope.response
        reviewed_by = response.get("reviewed_by", envelope.service_context.get("reviewed_by"))
        reviewer_identity = reviewed_by
        review_state, acceptance_state = "pending", "pending_review"
        qc = RegistrationQCRecord(
            schema_version=REGISTRATION_QC_SCHEMA_VERSION,
            qc_id=f"registration-qc-{registration_id.removeprefix('registration-')}",
            registration_id=registration_id,
            tissue_generation_id=generation.generation_id,
            sampling_context=RegistrationSamplingContext(
                target_frame="fixed_level0_pixels",
                sample_spacing_px=128.0,
                grid_origin_offset_px=(64.0, 64.0),
                z_position=0.0,
            ),
            policy=policy,
            global_evidence=global_evidence,
            section_records=tuple(section_records),
            computed_status=computed_status,
            review_state=review_state,
            reviewed_by=None,
        )
        record = RegistrationRecord.from_mapping(
            {
                "schema_version": REGISTRATION_SCHEMA_VERSION,
                "registration_id": registration_id,
                "provider": evidence["provider"],
                "provider_version": evidence["provider_version"],
                "moving_project_entry_id": evidence["moving_project_entry_id"],
                "fixed_project_entry_id": evidence["fixed_project_entry_id"],
                "moving_image_id": moving["image_id"],
                "fixed_image_id": generation.canonical_image_id,
                "moving_image_sha256": moving_sha256,
                "fixed_image_sha256": generation.canonical_image_sha256,
                "moving_image_path": moving["image_path"],
                "fixed_image_path": fixed["image_path"],
                "direction": "moving_to_fixed",
                "moving_physical_calibration": evidence["moving_physical_calibration"],
                "fixed_physical_calibration": evidence["fixed_physical_calibration"],
                "coordinate_pre_transform": evidence["coordinate_pre_transform"],
                "coordinate_post_transform": evidence["coordinate_post_transform"],
                "transform_file": evidence["transform_file"],
                "transform_file_sha256": evidence["transform_file_sha256"],
                "runtime_versions": evidence["runtime_versions"],
                "moving_modality": moving["moving_modality"],
                "moving_image_metadata": moving["source_metadata"],
                "tissue_generation_id": generation.generation_id,
                "tissue_section_ids": [item.section_id for item in generation.sections],
                "tissue_roi_ids": [item.roi_id for item in generation.sections],
                "tissue_path_object_ids": [item.tissue_path_object_id for item in section_records],
                "revision": revision,
                "supersedes_registration_id": supersedes,
                "predecessor_record_sha256": (lineage_predecessor_digest if revision > 1 else None),
                "acceptance_state": acceptance_state,
                "registration_qc": qc.to_mapping(),
            }
        )
        scope = RegistrationImportScope(
            moving_project_entry_id=str(moving["project_entry_id"]),
            fixed_project_entry_id=str(fixed["project_entry_id"]),
            moving_image_id=str(moving["image_id"]),
            fixed_image_id=generation.canonical_image_id,
            moving_image_sha256=moving_sha256,
            fixed_image_sha256=generation.canonical_image_sha256,
            moving_image_path=Path(str(moving["image_path"])),
            fixed_image_path=Path(str(fixed["image_path"])),
            transform_file=transform_file,
            transform_file_sha256=transform_sha256,
            tissue_generation_id=generation.generation_id,
            tissue_section_ids=tuple(item.section_id for item in generation.sections),
            tissue_roi_ids=tuple(item.roi_id for item in generation.sections),
            tissue_path_object_ids=tuple(item.path_object_id for item in generation.sections),
        )
        imported = import_registration_record(
            record.to_mapping(),
            expected_scope=scope,
            predecessor=lineage_predecessor if revision > 1 else None,
            predecessor_sha256=lineage_predecessor_digest if revision > 1 else None,
        )
        if pending_record is not None and registration_record_sha256(imported) != pending_digest:
            if _lineage_scope(imported) == _lineage_scope(pending_record):
                imported = _revise_pending_record(
                    imported,
                    revision=pending_record.revision + 1,
                    supersedes=pending_record.registration_id,
                    predecessor_sha256=pending_digest,
                )
                imported = import_registration_record(
                    imported.to_mapping(),
                    expected_scope=scope,
                    predecessor=pending_record,
                    predecessor_sha256=pending_digest,
                )
            return _registration_review_result(
                config,
                generation,
                imported,
                fixed,
                moving,
                predecessor=pending_record,
                predecessor_sha256=pending_digest,
                allow_object_transfer=self._allow_object_transfer,
            )
        if action in {"accept_registration", "accept_registration_and_transfer"}:
            if computed_status != "passed":
                return _failed(
                    config,
                    generation,
                    "registration_qc_not_acceptable",
                    "registration acceptance requires passing global and per-section QC",
                )
            imported = resolve_registration_review(
                imported, review_state="accepted", reviewed_by=reviewer_identity
            )
        elif action == "reject":
            imported = resolve_registration_review(
                imported, review_state="rejected", reviewed_by=reviewer_identity
            )
        else:
            return _registration_review_result(
                config,
                generation,
                imported,
                fixed,
                moving,
                allow_object_transfer=self._allow_object_transfer,
            )
        outputs: dict[str, object] = {
            "mode": "warpy",
            "registration": imported.to_mapping(),
            "moving_reference": dict(moving),
            "fixed_reference": dict(fixed),
            "transfer_qc_records": [],
            "transfer_commit_evidence": None,
            "transfer_cleanup_evidence": None,
            "transfer_attempts": [],
            "transfer_commit_outcomes": [],
        }
        if action == "accept_registration_and_transfer":
            raw_sources = response.get(
                "source_object_ids", envelope.service_context.get("source_object_ids")
            )
            if isinstance(raw_sources, (str, bytes)) or not isinstance(raw_sources, Sequence):
                raise ROIAnalysisError("transfer action requires source_object_ids")
            records = self._run_transfer_qc_admitted(
                project=project,
                registration=imported,
                generation=generation,
                source_object_ids=tuple(raw_sources),
                policy=TransferQCPolicy(**config.registration.transfer_qc_policy),
            )
            if any(item.authoritative for item in records):
                try:
                    outcome = self._commit_transfer_records_admitted(
                        project=project,
                        registration=imported,
                        source_object_ids=tuple(raw_sources),
                        records=records,
                    )
                except _TransferCommitFailure as exc:
                    return _transfer_commit_recovery_result(
                        config,
                        generation,
                        exc.attempt,
                        exc.failure,
                        fixed,
                        moving,
                        journal=exc.journal,
                        cleanup_required=exc.cleanup_required,
                    )
                except _OperationLockAuthorityFailure as exc:
                    return _transfer_commit_recovery_result(
                        config,
                        generation,
                        exc.journal.attempt,
                        exc,
                        fixed,
                        moving,
                        journal=exc.journal,
                        cleanup_required=True,
                    )
                except _WarpyContainmentManualReviewRequired as exc:
                    return _warpy_containment_manual_review_result(
                        config, generation, exc.journal, fixed, moving
                    )
                outputs["transfer_qc_records"] = [item.to_mapping() for item in outcome.records]
                outputs["transfer_commit_evidence"] = _mutable_json(outcome.commit_evidence)
                outputs["transfer_attempts"] = [outcome.attempt.to_mapping()]
                outputs["transfer_commit_outcomes"] = [
                    _mutable_json(item) for item in outcome.commit_outcomes
                ]
            elif any(item.computed_status == "passed" for item in records):
                proposal = build_transfer_review_proposal(
                    registration=imported,
                    source_object_ids=tuple(raw_sources),
                    qc_records=records,
                )
                reference = _persist_transfer_review_proposal(project, proposal)
                return _transfer_review_result(config, generation, fixed, moving, reference)
            outputs["transfer_qc_records"] = [item.to_mapping() for item in records]
        return StageResult.completed(
            stage="registration",
            slide_id=config.slide_id,
            generation_id=generation.generation_id,
            affected_object_ids=tuple(item.section_id for item in generation.sections),
            outputs=outputs,
        )

    def _resume_transfer_review(
        self,
        *,
        action: str,
        action_context: Mapping[str, Any],
        config: AnalysisConfig,
        generation: TissueSectionGeneration,
        project: object,
        fixed: Mapping[str, Any],
        moving: Mapping[str, Any],
    ) -> StageResult:
        if self._transfer_review_response_resolver is None or action_context:
            raise ROIAnalysisError("transfer review requires the retained authenticated resolver")
        try:
            resolver = authenticate_transfer_review_response_resolver(
                self._transfer_review_response_resolver
            )
        except TransferReviewAuthorityError as exc:
            raise ROIAnalysisError("transfer review resolver is not state-owner-issued") from exc
        (
            accepted_action,
            reviewed_by,
            accepted_response_sha256,
            context,
        ) = _resolved_transfer_review_response(
            resolver.resolve_transfer_review_response(
                project_file=str(_absolute_without_links(getattr(project, "project_file"))),
                moving_project_entry_id=str(moving["project_entry_id"]),
                fixed_project_entry_id=str(fixed["project_entry_id"]),
                tissue_generation_id=generation.generation_id,
            )
        )
        if accepted_action != action:
            raise ROIAnalysisError("transfer review action differs from accepted response")
        proposal = _transfer_review_proposal_from_reference(
            project, context["transfer_review_proposal_reference"]
        )
        proposal_sha256 = transfer_review_proposal_sha256(proposal)
        expected_project_file = str(_absolute_without_links(getattr(project, "project_file")))
        if (
            context["proposal_sha256"] != proposal_sha256
            or context["project_file"] != expected_project_file
            or context["project_file"] != fixed["project_file"]
            or context["moving_project_entry_id"] != moving["project_entry_id"]
            or context["fixed_project_entry_id"] != fixed["project_entry_id"]
            or context["registration_sha256"] != proposal.registration_sha256
            or context["source_object_count"] != len(proposal.source_object_ids)
            or context["qc_record_count"] != len(proposal.qc_records)
            or proposal.registration.tissue_generation_id != generation.generation_id
            or proposal.registration.tissue_section_ids
            != tuple(item.section_id for item in generation.sections)
            or proposal.registration.moving_project_entry_id != moving["project_entry_id"]
            or proposal.registration.fixed_project_entry_id != fixed["project_entry_id"]
            or proposal.registration.moving_image_sha256 != moving["image_sha256"]
            or proposal.registration.fixed_image_sha256 != fixed["image_sha256"]
            or proposal.qc_records[0].policy
            != TransferQCPolicy(**config.registration.transfer_qc_policy)
        ):
            raise ROIAnalysisError("transfer review proposal execution scope changed")
        _validate_registration_files(proposal.registration)
        decision = issue_transfer_review_decision(
            proposal,
            action=accepted_action,
            reviewed_by=reviewed_by,
            accepted_interaction_response_sha256=accepted_response_sha256,
        )
        decision_reference = _persist_transfer_review_decision(project, proposal, decision)
        common_outputs: dict[str, object] = {
            "mode": "warpy",
            "registration": proposal.registration.to_mapping(),
            "moving_reference": dict(moving),
            "fixed_reference": dict(fixed),
            "transfer_review_proposal": proposal.to_mapping(),
            "transfer_review_proposal_reference": dict(
                context["transfer_review_proposal_reference"]
            ),
            "transfer_review_decision": decision.to_mapping(),
            "transfer_review_decision_reference": decision_reference,
            "transfer_qc_records": [item.to_mapping() for item in decision.reviewed_qc_records],
            "transfer_cleanup_evidence": None,
        }
        if accepted_action == "reject_transfers":
            return StageResult.completed(
                stage="registration",
                slide_id=config.slide_id,
                generation_id=generation.generation_id,
                affected_object_ids=tuple(proposal.source_object_ids),
                outputs={
                    **common_outputs,
                    "transfer_commit_evidence": None,
                    "transfer_attempts": [],
                    "transfer_commit_outcomes": [],
                },
            )
        try:
            outcome = self._commit_transfer_records_admitted(
                project=project,
                registration=proposal.registration,
                source_object_ids=proposal.source_object_ids,
                records=decision.reviewed_qc_records,
                decision_reference=decision_reference,
            )
        except _TransferCommitFailure as exc:
            return _transfer_commit_recovery_result(
                config,
                generation,
                exc.attempt,
                exc.failure,
                fixed,
                moving,
                journal=exc.journal,
                cleanup_required=exc.cleanup_required,
                decision_reference=decision_reference,
            )
        except _OperationLockAuthorityFailure as exc:
            return _transfer_commit_recovery_result(
                config,
                generation,
                exc.journal.attempt,
                exc,
                fixed,
                moving,
                journal=exc.journal,
                cleanup_required=True,
                decision_reference=decision_reference,
            )
        except _WarpyContainmentManualReviewRequired as exc:
            return _warpy_containment_manual_review_result(
                config,
                generation,
                exc.journal,
                fixed,
                moving,
                decision_reference=decision_reference,
            )
        return StageResult.completed(
            stage="registration",
            slide_id=config.slide_id,
            generation_id=generation.generation_id,
            affected_object_ids=tuple(proposal.source_object_ids),
            outputs={
                **common_outputs,
                "transfer_attempt": outcome.attempt.to_mapping(),
                "transfer_attempts": [outcome.attempt.to_mapping()],
                "transfer_commit_evidence": _mutable_json(outcome.commit_evidence),
                "transfer_commit_outcomes": [
                    _mutable_json(item) for item in outcome.commit_outcomes
                ],
            },
        )

    def transfer_objects(
        self,
        *,
        project: object,
        registration: RegistrationRecord,
        generation: TissueSectionGeneration,
        source_object_ids: Sequence[str],
        policy: TransferQCPolicy,
    ) -> TransferReviewProposal | tuple[TransferQCRecord, ...]:
        """Run non-mutating QC and publish a proposal when any row passes."""
        if not self._allow_object_transfer:
            raise ROIAnalysisError("object transfer is unavailable in registration-only mode")
        with self._admitted_transfer():
            records = self._run_transfer_qc_admitted(
                project=project,
                registration=registration,
                generation=generation,
                source_object_ids=source_object_ids,
                policy=policy,
            )
            if any(item.authoritative for item in records):
                raise ROIAnalysisError("public transfer QC cannot consume authoritative rows")
            if not any(item.computed_status == "passed" for item in records):
                return records
            proposal = build_transfer_review_proposal(
                registration=registration,
                source_object_ids=source_object_ids,
                qc_records=records,
            )
            _persist_transfer_review_proposal(project, proposal)
            return proposal

    def _transfer_objects_admitted(
        self,
        *,
        project: object,
        registration: RegistrationRecord,
        generation: TissueSectionGeneration,
        source_object_ids: Sequence[str],
        policy: TransferQCPolicy,
    ) -> _TransferBatchResult:
        records = self._run_transfer_qc_admitted(
            project=project,
            registration=registration,
            generation=generation,
            source_object_ids=source_object_ids,
            policy=policy,
        )
        return self._commit_transfer_records_admitted(
            project=project,
            registration=registration,
            source_object_ids=source_object_ids,
            records=records,
        )

    def _run_transfer_qc_admitted(
        self,
        *,
        project: object,
        registration: RegistrationRecord,
        generation: TissueSectionGeneration,
        source_object_ids: Sequence[str],
        policy: TransferQCPolicy,
    ) -> tuple[TransferQCRecord, ...]:
        if not isinstance(policy, TransferQCPolicy):
            raise ROIAnalysisError("transfer QC policy is invalid")
        if (
            tuple(item.section_id for item in generation.sections)
            != registration.tissue_section_ids
        ):
            raise ROIAnalysisError("transfer generation differs from registration tissue scope")
        sources = tuple(source_object_ids)
        if (
            not sources
            or any(not isinstance(item, str) or not item.strip() for item in sources)
            or len(set(sources)) != len(sources)
        ):
            raise ROIAnalysisError("source object IDs must be nonempty and unique")
        request = {
            "schema_version": "qupath-warpy-transfer-request/v2",
            "transform_reader": transform_reader_request(),
            "project_file": str(Path(getattr(project, "project_file")).resolve()),
            "registration": registration.to_mapping(),
            "source_object_ids": list(sources),
            "accepted_tissue_sections": [
                {
                    "section_id": section.section_id,
                    "tissue_roi_id": section.roi_id,
                    "tissue_path_object_id": path_object_id,
                    "generation_id": generation.generation_id,
                    "generation_status": "accepted",
                    "review_status": "accepted",
                    "pipeline_stage": "tissue_detection",
                }
                for section, path_object_id in zip(
                    generation.sections, registration.tissue_path_object_ids, strict=True
                )
            ],
            "transfer_qc_policy": policy.to_mapping(),
        }
        method = getattr(self._runner, "transfer_warpy_objects", None)
        if not callable(method):
            raise ROIAnalysisError("QuPath runner cannot validate Warpy object transfers")
        _validate_registration_files(registration)
        batch = _required_mapping(
            method(project, request),
            {"records", "authoritative_object_ids"},
            "Warpy transfer batch",
        )
        _validate_registration_files(registration)
        if batch["authoritative_object_ids"] != []:
            raise ROIAnalysisError("Warpy transfer QC may not mutate the authoritative hierarchy")
        raw_records = batch["records"]
        if not isinstance(raw_records, list):
            raise ROIAnalysisError("raw Warpy transfer QC records must be a list")
        if any(
            isinstance(item, Mapping)
            and item.get("computed_status") == "passed"
            and (
                item.get("review_state") != "pending" or item.get("disposition") != "pending_review"
            )
            for item in raw_records
        ):
            raise ROIAnalysisError("raw Warpy transfer QC pass must remain pending review")
        records = import_transfer_qc_records(
            raw_records,
            registration=registration,
            tissue_generation_id=generation.generation_id,
            tissue_section_ids=registration.tissue_section_ids,
            policy=policy,
        )
        if tuple(item.source_object_id for item in records) != sources:
            raise ROIAnalysisError(
                "transfer QC must cover every requested source object exactly once"
            )
        return records

    def _commit_transfer_records_admitted(
        self,
        *,
        project: object,
        registration: RegistrationRecord,
        source_object_ids: Sequence[str],
        records: Sequence[TransferQCRecord],
        decision_reference: Mapping[str, object] | None = None,
    ) -> _TransferBatchResult:
        sources = tuple(source_object_ids)
        records = tuple(records)
        if (
            not records
            or tuple(item.source_object_id for item in records) != sources
            or any(not isinstance(item, TransferQCRecord) for item in records)
        ):
            raise ROIAnalysisError("reviewed transfer rows do not match source order")
        if any(
            item.registration_id != registration.registration_id
            or item.registration_revision != registration.revision
            or item.tissue_generation_id != registration.tissue_generation_id
            or item.moving_project_entry_id != registration.moving_project_entry_id
            or item.fixed_project_entry_id != registration.fixed_project_entry_id
            or item.policy != records[0].policy
            for item in records
        ):
            raise ROIAnalysisError("reviewed transfer rows changed registration scope")
        if any(item.authoritative for item in records) and decision_reference is None:
            raise ROIAnalysisError("authoritative transfer attempt requires a decision reference")
        _validate_registration_files(registration)
        project_file = str(_absolute_without_links(getattr(project, "project_file")))
        records_sha256 = hashlib.sha256(
            json.dumps(
                [item.to_mapping() for item in records],
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        attempt = TransferAttemptRecord(
            schema_version="warpy-transfer-attempt/v1",
            attempt_id=canonical_transfer_attempt_id(registration, records),
            registration=registration,
            source_object_ids=sources,
            qc_records=records,
            qc_records_sha256=records_sha256,
        )
        if decision_reference is not None:
            _transfer_review_decision_from_reference(
                project,
                decision_reference,
                attempt,
            )
        journal = _persist_transfer_attempt(
            project,
            attempt,
            retained_lock_authorities=self._retained_warpy_authority(project, attempt),
        )
        journal = self._remember_warpy_authority(journal)
        attempt_was_reused = journal.reused
        accepted = tuple(item for item in records if item.authoritative)
        if not accepted:
            with _transfer_attempt_operation_lock(journal):
                try:
                    journal = _recover_stale_transfer_lease(journal)
                except _LeaseReleaseFailure as exc:
                    raise _TransferCommitFailure(
                        attempt,
                        exc.failure,
                        cleanup_required=(
                            exc.journal.active_lease is not None
                            and exc.journal.active_lease["operation"] == "commit"
                        ),
                        journal=exc.journal,
                    ) from exc
                if journal.outcomes:
                    if [item["status"] for item in journal.outcomes] != ["not_required"]:
                        raise ROIAnalysisError(
                            "durable transfer attempt outcome conflicts with nonauthoritative QC"
                        )
                else:
                    journal = _append_transfer_journal_outcome(
                        journal,
                        _transfer_commit_outcome(attempt, sequence=1, status="not_required"),
                    )
            self._release_terminal_warpy_authority(journal)
            return _TransferBatchResult(
                records=records,
                commit_evidence=None,
                attempt=attempt,
                commit_outcomes=journal.outcomes,
            )
        with _transfer_attempt_operation_lock(journal):
            try:
                journal = _recover_stale_transfer_lease(journal)
            except _LeaseReleaseFailure as exc:
                journal = _append_transfer_failure_best_effort(
                    exc.journal,
                    attempt,
                    exc.failure,
                    cleanup_required=True,
                )
                raise _TransferCommitFailure(
                    attempt,
                    exc.failure,
                    cleanup_required=True,
                    journal=journal,
                ) from exc
            terminal_status = None if not journal.outcomes else journal.outcomes[-1]["status"]
            if terminal_status in {"not_required", "quarantined", "aborted"}:
                raise ROIAnalysisError(
                    "durable transfer attempt already has a terminal noncommit outcome"
                )
            if journal.cleanup_evidence is not None:
                raise ROIAnalysisError(
                    "durable transfer attempt has a newer target-absence attestation"
                )
            journal, lease = _acquire_transfer_lease(journal, operation="commit")
            try:
                evidence = self._commit_accepted_transfer(
                    project=project,
                    registration=registration,
                    records=accepted,
                    project_file=project_file,
                    attempt_id=attempt.attempt_id,
                    journal=journal,
                    lease=lease,
                )
            except _CommitBoundaryFailure as exc:
                cleanup_required = (
                    exc.cleanup_required
                    or attempt_was_reused
                    or journal.commit_evidence is not None
                )
                failure_mapping = _transfer_commit_failure(
                    attempt, exc.failure, cleanup_required=cleanup_required
                )
                try:
                    journal = _append_transfer_journal_outcome(
                        journal,
                        _transfer_commit_outcome(
                            attempt,
                            sequence=len(journal.outcomes) + 1,
                            status="failed",
                            failure=failure_mapping,
                        ),
                    )
                except Exception as publication_failure:
                    if cleanup_required:
                        journal, cleanup_failure = self._cleanup_after_lost_commit_ownership(
                            project=project,
                            attempt=attempt,
                            records=accepted,
                            project_file=project_file,
                            journal=journal,
                            lease=lease,
                            publication_failure=publication_failure,
                        )
                        raise _TransferCommitFailure(
                            attempt,
                            cleanup_failure or publication_failure,
                            cleanup_required=True,
                            journal=journal,
                        ) from publication_failure
                    journal, release_failure = _try_release_transfer_lease(
                        journal, lease, status="failed"
                    )
                    raise _TransferCommitFailure(
                        attempt,
                        release_failure or publication_failure,
                        cleanup_required=False,
                        journal=journal,
                    ) from publication_failure
                journal, release_failure = _try_release_transfer_lease(
                    journal, lease, status="failed"
                )
                raise _TransferCommitFailure(
                    attempt,
                    release_failure or exc.failure,
                    cleanup_required=cleanup_required,
                    journal=journal,
                ) from exc
            status = "committed" if journal.commit_evidence is None else "reconciled"
            try:
                journal = _append_transfer_journal_outcome(
                    journal,
                    _transfer_commit_outcome(
                        attempt,
                        sequence=len(journal.outcomes) + 1,
                        status=status,
                        live_attestation=evidence,
                    ),
                )
            except Exception as exc:
                journal, cleanup_failure = self._cleanup_after_lost_commit_ownership(
                    project=project,
                    attempt=attempt,
                    records=accepted,
                    project_file=project_file,
                    journal=journal,
                    lease=lease,
                    publication_failure=exc,
                )
                raise _TransferCommitFailure(
                    attempt,
                    cleanup_failure or exc,
                    cleanup_required=True,
                    journal=journal,
                ) from exc
            journal, release_failure = _try_release_transfer_lease(
                journal,
                lease,
                status="completed",
                post_mutation_evidence=evidence,
            )
            if release_failure is not None:
                journal = _append_transfer_failure_best_effort(
                    journal,
                    attempt,
                    release_failure,
                    cleanup_required=True,
                )
                raise _TransferCommitFailure(
                    attempt,
                    release_failure,
                    cleanup_required=True,
                    journal=journal,
                ) from release_failure
        return _TransferBatchResult(
            records=records,
            commit_evidence=(journal.commit_evidence or evidence),
            attempt=attempt,
            commit_outcomes=journal.outcomes,
        )

    def _commit_accepted_transfer(
        self,
        *,
        project: object,
        registration: RegistrationRecord,
        records: Sequence[TransferQCRecord],
        project_file: object,
        attempt_id: str,
        journal: _TransferAttemptJournal,
        lease: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        commit = getattr(self._runner, "commit_warpy_objects", None)
        if not callable(commit):
            raise _CommitBoundaryFailure(
                ROIAnalysisError("QuPath runner cannot commit validated Warpy transfers"),
                cleanup_required=False,
            )
        try:
            _validate_registration_files(registration)
        except Exception as exc:
            raise _CommitBoundaryFailure(exc, cleanup_required=False) from exc
        request = {
            "schema_version": "qupath-warpy-transfer-commit-request/v2",
            "transform_reader": transform_reader_request(),
            "project_file": project_file,
            "attempt_id": attempt_id,
            "registration": registration.to_mapping(),
            "records": [item.to_mapping() for item in records],
            "mutation_lease": _lease_request(lease),
        }
        recorder = _issue_warpy_containment_recorder(
            journal,
            lease,
            request_sha256=_json_sha256(request),
            project_identity_sha256=_warpy_project_identity_sha256(project_file),
            mutation_run_sha256=str(lease["lease_sha256"]),
        )
        try:
            with _transfer_hierarchy_lock(journal, lease):
                raw_evidence = commit(
                    project,
                    request,
                    containment_recorder=recorder,
                )
            evidence = _required_mapping(
                raw_evidence,
                {
                    "schema_version",
                    "attempt_id",
                    "registration_id",
                    "registration_revision",
                    "authoritative_object_ids",
                    "objects",
                },
                "Warpy transfer commit evidence",
            )
            _validate_registration_files(registration)
        except Exception as exc:
            raise _CommitBoundaryFailure(exc, cleanup_required=True) from exc
        if (
            set(evidence)
            != {
                "schema_version",
                "attempt_id",
                "registration_id",
                "registration_revision",
                "authoritative_object_ids",
                "objects",
            }
            or evidence["schema_version"] != "qupath-warpy-transfer-commit/v1"
            or evidence["attempt_id"] != attempt_id
            or evidence["registration_id"] != registration.registration_id
            or evidence["registration_revision"] != registration.revision
        ):
            raise _CommitBoundaryFailure(
                ROIAnalysisError("Warpy transfer commit attempt scope changed"),
                cleanup_required=True,
            )
        if evidence["authoritative_object_ids"] != [item.target_object_id for item in records]:
            raise _CommitBoundaryFailure(
                ROIAnalysisError("Warpy transfer commit scope does not match accepted QC"),
                cleanup_required=True,
            )
        objects = evidence["objects"]
        if isinstance(objects, (str, bytes)) or not isinstance(objects, Sequence):
            raise _CommitBoundaryFailure(
                ROIAnalysisError("Warpy transfer commit object evidence is invalid"),
                cleanup_required=True,
            )
        expected_objects = [
            {
                "source_object_id": item.source_object_id,
                "target_object_id": item.target_object_id,
                "target_section_id": item.target_section_id,
                "target_geometry_sha256": item.transformed_geometry_sha256,
            }
            for item in records
        ]
        if len(objects) != len(expected_objects):
            raise _CommitBoundaryFailure(
                ROIAnalysisError("Warpy transfer commit object evidence is incomplete"),
                cleanup_required=True,
            )
        for expected, actual in zip(expected_objects, objects, strict=True):
            try:
                row = _required_mapping(
                    actual,
                    {
                        "source_object_id",
                        "target_object_id",
                        "target_section_id",
                        "target_geometry_sha256",
                        "reconciled",
                    },
                    "Warpy transfer commit object evidence",
                )
            except Exception as exc:
                raise _CommitBoundaryFailure(exc, cleanup_required=True) from exc
            if any(row[name] != value for name, value in expected.items()):
                raise _CommitBoundaryFailure(
                    ROIAnalysisError("Warpy transfer commit live object evidence changed"),
                    cleanup_required=True,
                )
        return evidence

    def _cleanup_after_lost_commit_ownership(
        self,
        *,
        project: object,
        attempt: TransferAttemptRecord,
        records: Sequence[TransferQCRecord],
        project_file: object,
        journal: _TransferAttemptJournal,
        lease: Mapping[str, Any],
        publication_failure: Exception,
    ) -> tuple[_TransferAttemptJournal, Exception | None]:
        """Synchronously remove exact targets when commit publication cannot win."""
        journal_failure: Exception | None = None
        try:
            failure_mapping = _transfer_commit_failure(
                attempt, publication_failure, cleanup_required=True
            )
            journal = _append_transfer_journal_outcome(
                journal,
                _transfer_commit_outcome(
                    attempt,
                    sequence=len(journal.outcomes) + 1,
                    status="failed",
                    failure=failure_mapping,
                ),
            )
        except Exception as exc:
            journal_failure = exc
        try:
            journal = _release_transfer_lease(
                journal,
                lease,
                status="failed",
            )
            journal, cleanup_lease = _acquire_transfer_lease(
                journal,
                operation="cleanup",
            )
            cleanup_evidence = self._cleanup_transfer_attempt(
                project=project,
                attempt=attempt,
                records=records,
                project_file=project_file,
                journal=journal,
                lease=cleanup_lease,
            )
            journal = _append_transfer_journal_cleanup(journal, cleanup_evidence)
            journal, release_failure = _try_release_transfer_lease(
                journal,
                cleanup_lease,
                status="completed",
                post_mutation_evidence=cleanup_evidence,
            )
            return journal, release_failure or journal_failure
        except Exception as cleanup_failure:
            active = journal.active_lease
            if active is not None:
                journal, _release_failure = _try_release_transfer_lease(
                    journal, active, status="failed"
                )
            return journal, cleanup_failure

    def _resume_transfer_attempt(
        self,
        *,
        action: str,
        action_context: Mapping[str, Any],
        config: AnalysisConfig,
        generation: TissueSectionGeneration,
        project: object,
        fixed: Mapping[str, Any],
        moving: Mapping[str, Any],
    ) -> StageResult:
        context = resolve_resume_envelope(action_context).service_context
        attempt = _transfer_attempt_from_reference(
            project, context.get("transfer_attempt_reference")
        )
        digest = context.get("transfer_attempt_sha256")
        if not isinstance(digest, str) or transfer_attempt_sha256(attempt) != digest:
            raise ROIAnalysisError("checkpointed transfer attempt digest mismatch")
        if attempt.registration.tissue_generation_id != generation.generation_id:
            raise ROIAnalysisError("transfer recovery tissue generation changed")
        decision_reference = _transfer_review_decision_reference_from_context(
            project,
            action_context,
            attempt,
        )
        failure = _required_mapping(
            context.get("commit_failure"),
            {
                "schema_version",
                "attempt_id",
                "exception_type",
                "message",
                "mutation_state",
            },
            "transfer commit failure",
        )
        if failure["attempt_id"] != attempt.attempt_id:
            raise ROIAnalysisError("transfer commit failure attempt scope changed")
        failure_digest = context.get("commit_failure_sha256")
        expected_failure_digest = hashlib.sha256(
            json.dumps(dict(failure), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if failure_digest != expected_failure_digest:
            raise ROIAnalysisError("checkpointed transfer commit failure digest mismatch")
        try:
            journal = _load_transfer_attempt_journal(
                project,
                attempt,
                retained_lock_authorities=self._retained_warpy_authority(project, attempt),
            )
        except _WarpyContainmentManualReviewRequired as exc:
            return _warpy_containment_manual_review_result(
                config,
                generation,
                exc.journal,
                fixed,
                moving,
                decision_reference=decision_reference,
            )
        _validate_checkpointed_outcome_summary(context, journal.outcomes)
        accepted = tuple(item for item in attempt.qc_records if item.authoritative)
        desired_terminal = (
            "quarantined"
            if action == "quarantine_transfer_attempt"
            else "aborted" if action == "abort_transfer_attempt" else None
        )
        with _transfer_attempt_operation_lock(journal):
            try:
                journal = _recover_stale_transfer_lease(journal)
            except _WarpyContainmentManualReviewRequired as exc:
                return _warpy_containment_manual_review_result(
                    config,
                    generation,
                    exc.journal,
                    fixed,
                    moving,
                    decision_reference=decision_reference,
                )
            except _LeaseReleaseFailure as exc:
                return _transfer_commit_recovery_result(
                    config,
                    generation,
                    attempt,
                    exc.failure,
                    fixed,
                    moving,
                    journal=exc.journal,
                    cleanup_required=(
                        exc.journal.active_lease is not None
                        and exc.journal.active_lease["operation"] == "commit"
                    ),
                    publish_failure=True,
                    decision_reference=decision_reference,
                )
            _validate_checkpointed_outcome_summary(context, journal.outcomes)
            terminal_status = None if not journal.outcomes else journal.outcomes[-1]["status"]
            if terminal_status in {"not_required", "quarantined", "aborted"}:
                if desired_terminal == terminal_status:
                    return _completed_transfer_attempt_result(
                        config,
                        generation,
                        attempt,
                        fixed,
                        moving,
                        journal,
                        decision_reference=decision_reference,
                    )
                raise ROIAnalysisError(
                    "durable transfer attempt already has a different terminal outcome"
                )
            if action == "retry_transfer_commit":
                if journal.cleanup_evidence is not None:
                    raise ROIAnalysisError(
                        "cannot recommit after durable transfer-target absence attestation"
                    )
                journal, lease = _acquire_transfer_lease(journal, operation="commit")
                try:
                    current_evidence = self._commit_accepted_transfer(
                        project=project,
                        registration=attempt.registration,
                        records=accepted,
                        project_file=str(Path(getattr(project, "project_file")).resolve()),
                        attempt_id=attempt.attempt_id,
                        journal=journal,
                        lease=lease,
                    )
                except _CommitBoundaryFailure as exc:
                    cleanup_required = exc.cleanup_required or journal.commit_evidence is not None
                    failure_mapping = _transfer_commit_failure(
                        attempt, exc.failure, cleanup_required=cleanup_required
                    )
                    try:
                        journal = _append_transfer_journal_outcome(
                            journal,
                            _transfer_commit_outcome(
                                attempt,
                                sequence=len(journal.outcomes) + 1,
                                status="failed",
                                failure=failure_mapping,
                            ),
                        )
                    except Exception as publication_failure:
                        if cleanup_required:
                            journal, cleanup_failure = self._cleanup_after_lost_commit_ownership(
                                project=project,
                                attempt=attempt,
                                records=accepted,
                                project_file=str(Path(getattr(project, "project_file")).resolve()),
                                journal=journal,
                                lease=lease,
                                publication_failure=publication_failure,
                            )
                            return _transfer_commit_recovery_result(
                                config,
                                generation,
                                attempt,
                                cleanup_failure or publication_failure,
                                fixed,
                                moving,
                                journal=journal,
                                cleanup_required=True,
                                publish_failure=True,
                                decision_reference=decision_reference,
                            )
                        journal, release_failure = _try_release_transfer_lease(
                            journal, lease, status="failed"
                        )
                        return _transfer_commit_recovery_result(
                            config,
                            generation,
                            attempt,
                            release_failure or publication_failure,
                            fixed,
                            moving,
                            journal=journal,
                            cleanup_required=False,
                            publish_failure=True,
                            decision_reference=decision_reference,
                        )
                    journal, release_failure = _try_release_transfer_lease(
                        journal, lease, status="failed"
                    )
                    return _transfer_commit_recovery_result(
                        config,
                        generation,
                        attempt,
                        release_failure or exc.failure,
                        fixed,
                        moving,
                        journal=journal,
                        cleanup_required=cleanup_required,
                        publish_failure=True,
                        decision_reference=decision_reference,
                    )
                status = "committed" if journal.commit_evidence is None else "reconciled"
                try:
                    journal = _append_transfer_journal_outcome(
                        journal,
                        _transfer_commit_outcome(
                            attempt,
                            sequence=len(journal.outcomes) + 1,
                            status=status,
                            live_attestation=current_evidence,
                        ),
                    )
                except Exception as exc:
                    journal, cleanup_failure = self._cleanup_after_lost_commit_ownership(
                        project=project,
                        attempt=attempt,
                        records=accepted,
                        project_file=str(Path(getattr(project, "project_file")).resolve()),
                        journal=journal,
                        lease=lease,
                        publication_failure=exc,
                    )
                    return _transfer_commit_recovery_result(
                        config,
                        generation,
                        attempt,
                        cleanup_failure or exc,
                        fixed,
                        moving,
                        journal=journal,
                        cleanup_required=True,
                        publish_failure=True,
                        decision_reference=decision_reference,
                    )
                journal, release_failure = _try_release_transfer_lease(
                    journal,
                    lease,
                    status="completed",
                    post_mutation_evidence=current_evidence,
                )
                if release_failure is not None:
                    return _transfer_commit_recovery_result(
                        config,
                        generation,
                        attempt,
                        release_failure,
                        fixed,
                        moving,
                        journal=journal,
                        cleanup_required=True,
                        publish_failure=True,
                        decision_reference=decision_reference,
                    )
            else:
                journal, lease = _acquire_transfer_lease(journal, operation="cleanup")
                cleanup_required = (
                    failure["mutation_state"] == "indeterminate_or_persisted"
                    or journal.commit_evidence is not None
                    or any(
                        item["failure"]["mutation_state"] == "indeterminate_or_persisted"
                        for item in journal.outcomes
                        if item["status"] == "failed"
                    )
                )
                cleanup_evidence = journal.cleanup_evidence
                lease_cleanup_evidence = None
                if cleanup_required and cleanup_evidence is None:
                    try:
                        cleanup_evidence = self._cleanup_transfer_attempt(
                            project=project,
                            attempt=attempt,
                            records=accepted,
                            project_file=str(Path(getattr(project, "project_file")).resolve()),
                            journal=journal,
                            lease=lease,
                        )
                        lease_cleanup_evidence = cleanup_evidence
                        journal = _append_transfer_journal_cleanup(journal, cleanup_evidence)
                    except Exception as exc:
                        failure_mapping = _transfer_commit_failure(
                            attempt, exc, cleanup_required=True
                        )
                        journal = _append_transfer_journal_outcome(
                            journal,
                            _transfer_commit_outcome(
                                attempt,
                                sequence=len(journal.outcomes) + 1,
                                status="failed",
                                failure=failure_mapping,
                            ),
                        )
                        journal, release_failure = _try_release_transfer_lease(
                            journal, lease, status="failed"
                        )
                        return _transfer_commit_recovery_result(
                            config,
                            generation,
                            attempt,
                            release_failure or exc,
                            fixed,
                            moving,
                            journal=journal,
                            cleanup_required=True,
                            publish_failure=True,
                            decision_reference=decision_reference,
                        )
                try:
                    journal = _append_transfer_journal_outcome(
                        journal,
                        _transfer_commit_outcome(
                            attempt,
                            sequence=len(journal.outcomes) + 1,
                            status=desired_terminal,
                            failure=failure,
                        ),
                    )
                except Exception as exc:
                    journal, release_failure = _try_release_transfer_lease(
                        journal,
                        lease,
                        status="failed",
                        post_mutation_evidence=lease_cleanup_evidence,
                    )
                    return _transfer_commit_recovery_result(
                        config,
                        generation,
                        attempt,
                        release_failure or exc,
                        fixed,
                        moving,
                        journal=journal,
                        cleanup_required=cleanup_required,
                        publish_failure=True,
                        decision_reference=decision_reference,
                    )
                journal, release_failure = _try_release_transfer_lease(
                    journal,
                    lease,
                    status="completed",
                    post_mutation_evidence=lease_cleanup_evidence,
                )
                if release_failure is not None:
                    return _transfer_commit_recovery_result(
                        config,
                        generation,
                        attempt,
                        release_failure,
                        fixed,
                        moving,
                        journal=journal,
                        cleanup_required=cleanup_required,
                        publish_failure=True,
                        decision_reference=decision_reference,
                    )
        self._release_terminal_warpy_authority(journal)
        return _completed_transfer_attempt_result(
            config,
            generation,
            attempt,
            fixed,
            moving,
            journal,
            decision_reference=decision_reference,
        )

    def _cleanup_transfer_attempt(
        self,
        *,
        project: object,
        attempt: TransferAttemptRecord,
        records: Sequence[TransferQCRecord],
        project_file: object,
        journal: _TransferAttemptJournal,
        lease: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        cleanup = getattr(self._runner, "cleanup_warpy_objects", None)
        if not callable(cleanup):
            raise ROIAnalysisError("QuPath runner cannot prove transfer targets absent")
        request = {
            "schema_version": "qupath-warpy-transfer-cleanup-request/v1",
            "project_file": project_file,
            "attempt_id": attempt.attempt_id,
            "registration": attempt.registration.to_mapping(),
            "records": [item.to_mapping() for item in records],
            "mutation_lease": _lease_request(lease),
        }
        recorder = _issue_warpy_containment_recorder(
            journal,
            lease,
            request_sha256=_json_sha256(request),
            project_identity_sha256=_warpy_project_identity_sha256(project_file),
            mutation_run_sha256=str(lease["lease_sha256"]),
        )
        with _transfer_hierarchy_lock(journal, lease):
            raw_evidence = cleanup(
                project,
                request,
                containment_recorder=recorder,
            )
        evidence = _required_mapping(
            raw_evidence,
            {
                "schema_version",
                "attempt_id",
                "registration_id",
                "registration_revision",
                "absent_target_object_ids",
                "objects",
            },
            "Warpy transfer cleanup evidence",
        )
        return _validate_transfer_cleanup_evidence(attempt, evidence)


def _accepted_generation(state: Mapping[str, Any]) -> TissueSectionGeneration:
    raw = state.get("tissue_acceptance")
    if not isinstance(raw, Mapping):
        raise ROIAnalysisError("registration requires tissue-acceptance state")
    generation = TissueSectionGeneration.from_mapping(raw.get("generation"))
    if generation.status is not GenerationStatus.ACCEPTED or not generation.sections:
        raise ROIAnalysisError("registration requires an accepted nonempty tissue generation")
    return generation


_TRANSFER_ATTEMPT_ID = re.compile(r"transfer-attempt-[0-9a-f]{64}\Z")
_TRANSFER_REVIEW_PROPOSAL_ID = re.compile(r"transfer-review-proposal-[0-9a-f]{64}\Z")
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_SECURE_DIRECTORY_API = (
    os.name == "posix"
    and bool(_NOFOLLOW)
    and bool(getattr(os, "O_DIRECTORY", 0))
    and all(item in os.supports_dir_fd for item in (os.open, os.stat, os.mkdir, os.unlink, os.link))
    and os.rename in os.supports_dir_fd
)


def _validate_transfer_attempt_id(value: object) -> str:
    if not isinstance(value, str) or _TRANSFER_ATTEMPT_ID.fullmatch(value) is None:
        raise ROIAnalysisError("transfer attempt ID is not in canonical format")
    return value


def _absolute_without_links(path: object) -> Path:
    """Normalize lexically; resolving here would silently follow attacker links."""
    return Path(os.path.abspath(os.fspath(path)))


def _directory_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ROIAnalysisError("trusted Warpy transfer store component is not a directory")
    return metadata.st_dev, metadata.st_ino


def _require_secure_directory_api() -> None:
    if not _SECURE_DIRECTORY_API:
        backend = secure_store_backend()
        if backend is not None:
            with backend.open_directory(Path.cwd()):
                pass
        raise ROIAnalysisError("platform cannot prove no-follow Warpy transfer store identity")


def _windows_store_backend() -> WindowsSecureStoreBackend | None:
    backend = secure_store_backend()
    if backend is None:
        return None
    if isinstance(backend, UnsupportedSecureStoreBackend):
        with backend.open_directory(Path.cwd()):
            pass
        raise AssertionError("unreachable")
    return backend


def _secure_directory_identity(
    path: Path,
    *,
    create: bool = False,
    expected_identity: FileIdentity | tuple[int, int] | None = None,
) -> FileIdentity | tuple[int, int]:
    backend = _windows_store_backend()
    if backend is not None:
        expected = (
            None
            if expected_identity is None
            else _as_windows_identity(expected_identity, "directory identity")
        )
        return backend.directory_identity(path, create=create, expected_identity=expected)
    expected_posix = _as_posix_tuple(expected_identity, "directory identity")
    with _open_directory_no_follow(path, create=create, expected_identity=expected_posix) as (
        _descriptor,
        identity,
    ):
        return identity


def _secure_list_names(
    path: Path, *, expected_identity: FileIdentity | tuple[int, int]
) -> tuple[str, ...]:
    backend = _windows_store_backend()
    if backend is not None:
        expected = _as_windows_identity(expected_identity, "directory identity")
        with backend.open_directory(path, expected_identity=expected) as directory:
            return backend.list_names(directory)
    expected_posix = _as_posix_tuple(expected_identity, "directory identity")
    with _open_directory_no_follow(path, expected_identity=expected_posix) as (
        descriptor,
        _identity,
    ):
        return tuple(os.listdir(descriptor))


def _secure_regular_child_identity(
    directory: Path,
    name: str,
    *,
    expected_directory_identity: FileIdentity | tuple[int, int],
) -> FileIdentity | tuple[int, int] | None:
    backend = _windows_store_backend()
    if backend is not None:
        expected = _as_windows_identity(expected_directory_identity, "directory identity")
        with backend.open_directory(directory, expected_identity=expected) as opened:
            return backend.child_identity(opened, name)
    expected_posix = _as_posix_tuple(expected_directory_identity, "directory identity")
    with _open_directory_no_follow(directory, expected_identity=expected_posix) as (
        descriptor,
        _identity,
    ):
        metadata = _secure_child_metadata(descriptor, name)
        return None if metadata is None else _regular_file_identity(metadata)


@contextmanager
def _exclusive_posix_file_lock(descriptor: int):
    """Import and use ``flock`` only after the POSIX backend is proven."""
    _require_secure_directory_api()
    import fcntl  # POSIX-only and intentionally lazy

    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def _open_directory_no_follow(
    path: Path,
    *,
    create: bool = False,
    expected_identity: tuple[int, int] | None = None,
):
    """Walk and pin an absolute directory using no-follow directory descriptors."""
    _require_secure_directory_api()
    absolute = _absolute_without_links(path)
    descriptor = os.open(absolute.anchor, _DIRECTORY_OPEN_FLAGS | _NOFOLLOW)
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(
                    component,
                    _DIRECTORY_OPEN_FLAGS | _NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(
                    component,
                    _DIRECTORY_OPEN_FLAGS | _NOFOLLOW,
                    dir_fd=descriptor,
                )
            except (NotADirectoryError, OSError) as exc:
                raise ROIAnalysisError(
                    "Warpy transfer store path contains a symbolic link or unsafe component"
                ) from exc
            os.close(descriptor)
            descriptor = child
        identity = _directory_identity(descriptor)
        if expected_identity is not None and identity != expected_identity:
            raise ROIAnalysisError("Warpy transfer store directory identity changed")
        yield descriptor, identity
    finally:
        os.close(descriptor)


def _secure_child_metadata(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        raise ROIAnalysisError("Warpy transfer store file cannot be a symbolic link")
    return metadata


def _regular_file_identity(metadata: os.stat_result) -> tuple[int, int]:
    if not stat.S_ISREG(metadata.st_mode):
        raise ROIAnalysisError("Warpy transfer authority is not a regular file")
    return metadata.st_dev, metadata.st_ino


def _secure_open_regular_file(
    directory_fd: int,
    name: str,
    flags: int,
    *,
    expected_identity: tuple[int, int] | None = None,
    create: bool = False,
) -> tuple[int, tuple[int, int], bool]:
    before = _secure_child_metadata(directory_fd, name)
    if before is None and not create:
        raise FileNotFoundError(name)
    descriptor = os.open(
        name,
        flags | _NOFOLLOW | (os.O_CREAT if create else 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        opened_identity = _regular_file_identity(os.fstat(descriptor))
        after = _secure_child_metadata(directory_fd, name)
        if after is None:
            raise ROIAnalysisError("Warpy transfer authority identity changed")
        identities = {
            opened_identity,
            _regular_file_identity(after),
        }
        if before is not None:
            identities.add(_regular_file_identity(before))
        if expected_identity is not None:
            identities.add(expected_identity)
        if len(identities) != 1:
            raise ROIAnalysisError("Warpy transfer lock identity changed")
        return descriptor, opened_identity, before is None
    except Exception:
        os.close(descriptor)
        raise


def _ensure_operation_lock(
    store: Path,
    attempt_id: str,
    *,
    store_identity: FileIdentity | tuple[int, int],
    expected_identity: FileIdentity | tuple[int, int] | None = None,
) -> FileIdentity | tuple[int, int]:
    name = f"{attempt_id}.operation.lock"
    backend = _windows_store_backend()
    if backend is not None:
        backend.directory_identity(
            store,
            expected_identity=_as_windows_identity(
                store_identity, "transfer attempt store identity"
            ),
        )
        return backend.ensure_regular_file(
            store / name,
            expected_identity=(
                None
                if expected_identity is None
                else _as_windows_identity(expected_identity, "transfer operation lock identity")
            ),
        )
    with _open_directory_no_follow(
        store,
        expected_identity=_as_posix_tuple(store_identity, "transfer attempt store identity"),
    ) as (directory_fd, _identity):
        descriptor, identity, created = _secure_open_regular_file(
            directory_fd,
            name,
            os.O_RDWR,
            expected_identity=_as_posix_tuple(
                expected_identity, "transfer operation lock identity"
            ),
            create=True,
        )
        try:
            if created:
                os.fsync(descriptor)
                os.fsync(directory_fd)
            return identity
        finally:
            os.close(descriptor)


def _ensure_lock_path(
    path: Path,
    *,
    expected_identity: FileIdentity | tuple[int, int] | None = None,
) -> FileIdentity | tuple[int, int]:
    backend = _windows_store_backend()
    if backend is not None:
        return backend.ensure_regular_file(
            path,
            expected_identity=(
                None
                if expected_identity is None
                else _as_windows_identity(expected_identity, "file identity")
            ),
        )
    with _open_directory_no_follow(path.parent, create=True) as (directory_fd, _identity):
        descriptor, identity, created = _secure_open_regular_file(
            directory_fd,
            path.name,
            os.O_RDWR,
            expected_identity=_as_posix_tuple(expected_identity, "file identity"),
            create=True,
        )
        try:
            if created:
                os.fsync(descriptor)
                os.fsync(directory_fd)
            return identity
        finally:
            os.close(descriptor)


def _authenticate_existing_lock_path(
    path: Path,
    *,
    expected_identity: FileIdentity | tuple[int, int],
) -> FileIdentity | tuple[int, int]:
    """Authenticate one existing lock authority without creating any path."""
    backend = _windows_store_backend()
    if backend is not None:
        with backend.open_directory(path.parent, create=False) as directory:
            identity = backend.child_identity(directory, path.name)
        expected = _as_windows_identity(expected_identity, "file identity")
        if identity is None or identity != expected:
            raise ROIAnalysisError("Warpy transfer lock identity changed")
        return identity
    try:
        with _open_directory_no_follow(path.parent, create=False) as (
            directory_fd,
            _identity,
        ):
            descriptor, identity, created = _secure_open_regular_file(
                directory_fd,
                path.name,
                os.O_RDWR,
                expected_identity=_as_posix_tuple(expected_identity, "file identity"),
                create=False,
            )
            try:
                if created:
                    raise ROIAnalysisError("Warpy transfer lock was recreated")
                return identity
            finally:
                os.close(descriptor)
    except FileNotFoundError as exc:
        raise ROIAnalysisError("Warpy transfer lock authority is missing") from exc


def _retain_existing_posix_lock(
    path: Path,
    *,
    expected_identity: FileIdentity | tuple[int, int],
) -> _RetainedPosixLockAuthority:
    expected = _as_posix_tuple(expected_identity, "file identity")
    try:
        with _open_directory_no_follow(path.parent, create=False) as (
            directory_fd,
            _identity,
        ):
            descriptor, identity, created = _secure_open_regular_file(
                directory_fd,
                path.name,
                os.O_RDWR,
                expected_identity=expected,
                create=False,
            )
    except FileNotFoundError as exc:
        raise ROIAnalysisError("Warpy transfer lock authority is missing") from exc
    if created:
        os.close(descriptor)
        raise ROIAnalysisError("Warpy transfer lock was recreated")
    try:
        import fcntl

        retained_descriptor = fcntl.fcntl(
            descriptor,
            fcntl.F_DUPFD_CLOEXEC,
            64,
        )
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)
    descriptor = retained_descriptor
    authority = _RetainedPosixLockAuthority(path, descriptor, identity)
    try:
        authority.validate()
    except BaseException:
        authority.close()
        raise
    return authority


def _identity_mapping(
    identity: FileIdentity | tuple[int, int], *, tagged: bool = False
) -> dict[str, object]:
    if isinstance(identity, WindowsFileIdentity):
        return identity.to_mapping()
    if isinstance(identity, PosixFileIdentity):
        return identity.to_tagged_mapping() if tagged else identity.to_mapping()
    posix = PosixFileIdentity(identity[0], identity[1])
    return posix.to_tagged_mapping() if tagged else posix.to_mapping()


def _as_posix_tuple(
    identity: FileIdentity | tuple[int, int] | None, label: str
) -> tuple[int, int] | None:
    if identity is None:
        return None
    if isinstance(identity, tuple):
        return identity
    if isinstance(identity, PosixFileIdentity):
        return identity.device, identity.inode
    raise ROIAnalysisError(f"{label} is not a POSIX identity")


def _as_windows_identity(
    identity: FileIdentity | tuple[int, int], label: str
) -> WindowsFileIdentity:
    if not isinstance(identity, WindowsFileIdentity):
        raise ROIAnalysisError(f"{label} is not a Windows identity")
    return identity


def _stored_operation_lock_identity(
    rendered: str, attempt: TransferAttemptRecord
) -> FileIdentity | tuple[int, int]:
    """

    Reads the lock identity from the current saved annotation-transfer attempt.

    We check the complete envelope, its embedded attempt and the attempt checksum
    before reading its platform-specific file identity. POSIX device/inode values
    become the tuple used by the lock functions; Windows retains its volume and
    file-ID record.

    Args:
        rendered: Encoded current transfer-attempt envelope.
        attempt: Transfer attempt already selected for this operation.

    Returns:
        File identity identifying the operation's existing lock.

    Raises:
        ROIAnalysisError: JSON, format, attempt or checksum differs from the
            selected current attempt.
    """

    try:
        envelope = json.loads(rendered)
    except json.JSONDecodeError as error:
        raise ROIAnalysisError("The saved transfer attempt is not valid JSON.") from error
    fields = {"schema_version", "attempt", "attempt_sha256", "operation_lock_identity"}
    if (
        not isinstance(envelope, Mapping) or set(envelope) != fields
        or envelope["schema_version"] != "warpy-transfer-attempt-envelope/v3"
        or envelope["attempt"] != attempt.to_mapping()
        or envelope["attempt_sha256"] != transfer_attempt_sha256(attempt)
    ):
        raise ROIAnalysisError("The saved transfer attempt does not match the current format and selected operation.")
    assert_current_writer("warpy-transfer-attempt-envelope", "3")
    identity = file_identity_from_mapping(envelope["operation_lock_identity"])
    if isinstance(identity, PosixFileIdentity):
        if set(envelope["operation_lock_identity"]) != {"schema_version", "device", "inode"}:
            raise ROIAnalysisError("The saved POSIX lock identity is incomplete.")
        return identity.device, identity.inode
    return identity


def _secure_read_text(
    directory: Path,
    name: str,
    *,
    expected_identity: FileIdentity | tuple[int, int] | None = None,
) -> str:
    backend = _windows_store_backend()
    if backend is not None:
        expected = (
            None
            if expected_identity is None
            else _as_windows_identity(expected_identity, "directory identity")
        )
        with backend.open_directory(directory, expected_identity=expected) as opened:
            return backend.read_text(opened, name)
    with _open_directory_no_follow(
        directory,
        expected_identity=_as_posix_tuple(expected_identity, "directory identity"),
    ) as (directory_fd, _identity):
        before = _secure_child_metadata(directory_fd, name)
        if before is None or not stat.S_ISREG(before.st_mode):
            raise FileNotFoundError(name)
        try:
            descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=directory_fd)
        except OSError as exc:
            raise ROIAnalysisError(
                "Warpy transfer store file cannot be opened without following links"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            after = _secure_child_metadata(directory_fd, name)
            file_keys = {
                (before.st_dev, before.st_ino),
                (opened.st_dev, opened.st_ino),
                None if after is None else (after.st_dev, after.st_ino),
            }
            if not stat.S_ISREG(opened.st_mode) or after is None or len(file_keys) != 1:
                raise ROIAnalysisError("Warpy transfer store file identity changed")
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as stream:
                descriptor = -1
                return stream.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _secure_write_new(
    directory: Path,
    destination_name: str,
    rendered: str,
    *,
    expected_identity: FileIdentity | tuple[int, int] | None = None,
) -> bool:
    """Publish immutable bytes with a hard link; return False on exact reuse."""
    backend = _windows_store_backend()
    if backend is not None:
        expected = (
            None
            if expected_identity is None
            else _as_windows_identity(expected_identity, "directory identity")
        )
        with backend.open_directory(directory, expected_identity=expected) as opened:
            return backend.write_new_text(opened, destination_name, rendered)
    temporary_name = f".{destination_name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    with _open_directory_no_follow(
        directory,
        expected_identity=_as_posix_tuple(expected_identity, "directory identity"),
    ) as (directory_fd, _identity):
        if _secure_child_metadata(directory_fd, destination_name) is not None:
            return False
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
                descriptor = -1
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(
                    temporary_name,
                    destination_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                os.fsync(directory_fd)
                return True
            except FileExistsError:
                _secure_child_metadata(directory_fd, destination_name)
                return False
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _secure_replace_text(
    directory: Path,
    destination_name: str,
    rendered: str,
    *,
    expected_identity: FileIdentity | tuple[int, int] | None = None,
) -> None:
    backend = _windows_store_backend()
    if backend is not None:
        expected = (
            None
            if expected_identity is None
            else _as_windows_identity(expected_identity, "directory identity")
        )
        with backend.open_directory(directory, expected_identity=expected) as opened:
            backend.replace_text(opened, destination_name, rendered)
        return
    temporary_name = f".{destination_name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    with _open_directory_no_follow(
        directory,
        expected_identity=_as_posix_tuple(expected_identity, "directory identity"),
    ) as (directory_fd, _identity):
        _secure_child_metadata(directory_fd, destination_name)
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
                descriptor = -1
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            _secure_child_metadata(directory_fd, destination_name)
            os.replace(
                temporary_name,
                destination_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _transfer_attempt_store_paths(
    project: object, attempt: TransferAttemptRecord
) -> tuple[Path, Path]:
    attempt_id = _validate_transfer_attempt_id(attempt.attempt_id)
    project_file = _absolute_without_links(getattr(project, "project_file"))
    project_data_path = project_file.parent / "data"
    configured_data_path = _absolute_without_links(getattr(project, "data_dir", project_data_path))
    if configured_data_path != project_data_path:
        raise ROIAnalysisError("Warpy project data directory scope changed")
    directory = project_data_path / "registration-transfer-attempts"
    try:
        _secure_directory_identity(directory, create=True)
    except ROIAnalysisError:
        raise
    except OSError as exc:
        raise ROIAnalysisError(
            "Warpy transfer journal directory cannot be a symbolic link"
        ) from exc
    return (
        directory / f"{attempt_id}.json",
        directory / f"{attempt_id}.journal",
    )


def _transfer_review_directory(
    project: object,
    proposal_id: object,
    *,
    create: bool,
) -> Path:
    if (
        not isinstance(proposal_id, str)
        or _TRANSFER_REVIEW_PROPOSAL_ID.fullmatch(proposal_id) is None
    ):
        raise ROIAnalysisError("transfer review proposal ID is not canonical")
    project_file = _absolute_without_links(getattr(project, "project_file"))
    expected_data = project_file.parent / "data"
    configured_data = _absolute_without_links(getattr(project, "data_dir", expected_data))
    if configured_data != expected_data:
        raise ROIAnalysisError("Warpy project data directory scope changed")
    directory = expected_data / "registration-transfer-reviews" / proposal_id
    try:
        _secure_directory_identity(directory, create=create)
    except ROIAnalysisError:
        raise
    except OSError as exc:
        raise ROIAnalysisError("Warpy transfer review directory cannot be a symbolic link") from exc
    return directory


def _persist_transfer_review_proposal(
    project: object,
    proposal: TransferReviewProposal,
) -> dict[str, object]:
    if not isinstance(proposal, TransferReviewProposal):
        raise ROIAnalysisError("transfer review publication requires a proposal")
    directory = _transfer_review_directory(project, proposal.proposal_id, create=True)
    identity = _secure_directory_identity(directory)
    rendered = json.dumps(
        proposal.to_mapping(),
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload = rendered.encode("utf-8")
    if not 1 <= len(payload) <= 1_048_576:
        raise ROIAnalysisError("transfer review proposal is unbounded")
    created = _secure_write_new(directory, "proposal.json", rendered, expected_identity=identity)
    if (
        not created
        and _secure_read_text(directory, "proposal.json", expected_identity=identity) != rendered
    ):
        raise ROIAnalysisError("transfer review proposal publication conflict")
    return {
        "schema_version": "warpy-transfer-review-proposal-reference/v1",
        "proposal_id": proposal.proposal_id,
        "path": str(directory / "proposal.json"),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_count": len(payload),
        "registration_sha256": proposal.registration_sha256,
        "source_object_count": len(proposal.source_object_ids),
        "qc_record_count": len(proposal.qc_records),
        "qc_records_sha256": proposal.qc_records_sha256,
    }


def _transfer_review_proposal_from_reference(
    project: object,
    value: object,
) -> TransferReviewProposal:
    reference = _required_mapping(
        value,
        {
            "schema_version",
            "proposal_id",
            "path",
            "sha256",
            "byte_count",
            "registration_sha256",
            "source_object_count",
            "qc_record_count",
            "qc_records_sha256",
        },
        "transfer review proposal reference",
    )
    if reference["schema_version"] != ("warpy-transfer-review-proposal-reference/v1"):
        raise ROIAnalysisError("transfer review proposal reference is unsupported")
    directory = _transfer_review_directory(project, reference["proposal_id"], create=False)
    path = directory / "proposal.json"
    if reference["path"] != str(path):
        raise ROIAnalysisError("transfer review proposal reference path changed")
    identity = _secure_directory_identity(directory)
    rendered = _secure_read_text(directory, path.name, expected_identity=identity)
    payload = rendered.encode("utf-8")
    if (
        type(reference["byte_count"]) is not int
        or not 1 <= reference["byte_count"] <= 1_048_576
        or len(payload) != reference["byte_count"]
        or hashlib.sha256(payload).hexdigest() != reference["sha256"]
    ):
        raise ROIAnalysisError("transfer review proposal reference bytes changed")
    try:
        decoded = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise ROIAnalysisError("transfer review proposal JSON is invalid") from exc
    if (
        json.dumps(
            decoded,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        != rendered
    ):
        raise ROIAnalysisError("transfer review proposal bytes are not canonical")
    proposal = TransferReviewProposal.from_mapping(decoded)
    if (
        transfer_review_proposal_sha256(proposal) != reference["sha256"]
        or proposal.registration_sha256 != reference["registration_sha256"]
        or len(proposal.source_object_ids) != reference["source_object_count"]
        or len(proposal.qc_records) != reference["qc_record_count"]
        or len(proposal.source_object_ids) != len(proposal.qc_records)
        or proposal.qc_records_sha256 != reference["qc_records_sha256"]
    ):
        raise ROIAnalysisError("transfer review proposal reference scope changed")
    return proposal


def _persist_transfer_review_decision(
    project: object,
    proposal: TransferReviewProposal,
    decision: TransferReviewDecision,
) -> dict[str, object]:
    if (
        not isinstance(decision, TransferReviewDecision)
        or decision.proposal_id != proposal.proposal_id
        or decision.proposal_sha256 != transfer_review_proposal_sha256(proposal)
    ):
        raise ROIAnalysisError("transfer review decision proposal scope changed")
    directory = _transfer_review_directory(project, proposal.proposal_id, create=False)
    identity = _secure_directory_identity(directory)
    rendered = json.dumps(
        decision.to_mapping(),
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload = rendered.encode("utf-8")
    if not 1 <= len(payload) <= 1_048_576:
        raise ROIAnalysisError("transfer review decision is unbounded")
    created = _secure_write_new(directory, "decision.json", rendered, expected_identity=identity)
    if (
        not created
        and _secure_read_text(directory, "decision.json", expected_identity=identity) != rendered
    ):
        raise ROIAnalysisError("transfer review decision publication conflict")
    return {
        "schema_version": "warpy-transfer-review-decision-reference/v1",
        "decision_id": decision.decision_id,
        "path": str(directory / "decision.json"),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_count": len(payload),
        "proposal_id": proposal.proposal_id,
        "proposal_sha256": decision.proposal_sha256,
        "accepted_interaction_response_sha256": (decision.accepted_interaction_response_sha256),
        "reviewed_qc_records_sha256": decision.reviewed_qc_records_sha256,
    }


def _transfer_review_decision_reference_from_context(
    project: object,
    action_context: Mapping[str, Any],
    attempt: TransferAttemptRecord,
) -> dict[str, object] | None:
    context = resolve_resume_envelope(action_context).service_context
    raw = context.get("transfer_review_decision_reference")
    if raw is None:
        if attempt.schema_version == "warpy-transfer-attempt/v1" and any(
            item.authoritative for item in attempt.qc_records
        ):
            raise ROIAnalysisError("authoritative transfer recovery requires a decision reference")
        return None
    reference = dict(
        _required_mapping(
            raw,
            {
                "schema_version",
                "decision_id",
                "path",
                "sha256",
                "byte_count",
                "proposal_id",
                "proposal_sha256",
                "accepted_interaction_response_sha256",
                "reviewed_qc_records_sha256",
            },
            "transfer review decision reference",
        )
    )
    _transfer_review_decision_from_reference(project, reference, attempt)
    return reference


def _transfer_review_decision_from_reference(
    project: object,
    value: object,
    attempt: TransferAttemptRecord,
) -> TransferReviewDecision:
    reference = _required_mapping(
        value,
        {
            "schema_version",
            "decision_id",
            "path",
            "sha256",
            "byte_count",
            "proposal_id",
            "proposal_sha256",
            "accepted_interaction_response_sha256",
            "reviewed_qc_records_sha256",
        },
        "transfer review decision reference",
    )
    if reference["schema_version"] != ("warpy-transfer-review-decision-reference/v1"):
        raise ROIAnalysisError("transfer review decision reference is unsupported")
    directory = _transfer_review_directory(project, reference["proposal_id"], create=False)
    path = directory / "decision.json"
    if reference["path"] != str(path):
        raise ROIAnalysisError("transfer review decision reference path changed")
    identity = _secure_directory_identity(directory)
    rendered = _secure_read_text(directory, path.name, expected_identity=identity)
    payload = rendered.encode("utf-8")
    if (
        type(reference["byte_count"]) is not int
        or not 1 <= reference["byte_count"] <= 1_048_576
        or len(payload) != reference["byte_count"]
        or hashlib.sha256(payload).hexdigest() != reference["sha256"]
    ):
        raise ROIAnalysisError("transfer review decision reference bytes changed")
    try:
        decoded = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise ROIAnalysisError("transfer review decision JSON is invalid") from exc
    if (
        json.dumps(
            decoded,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        != rendered
    ):
        raise ROIAnalysisError("transfer review decision bytes are not canonical")
    decision = TransferReviewDecision.from_mapping(decoded)
    if (
        decision.decision_id != reference["decision_id"]
        or decision.proposal_id != reference["proposal_id"]
        or decision.proposal_sha256 != reference["proposal_sha256"]
        or decision.accepted_interaction_response_sha256
        != reference["accepted_interaction_response_sha256"]
        or decision.reviewed_qc_records_sha256 != reference["reviewed_qc_records_sha256"]
        or decision.reviewed_qc_records != attempt.qc_records
        or attempt.registration.registration_id != decision.reviewed_qc_records[0].registration_id
    ):
        raise ROIAnalysisError("transfer review decision reference scope changed")
    proposal_rendered = _secure_read_text(directory, "proposal.json", expected_identity=identity)
    try:
        proposal_mapping = json.loads(proposal_rendered)
    except json.JSONDecodeError as exc:
        raise ROIAnalysisError("transfer review proposal JSON is invalid") from exc
    canonical_proposal = json.dumps(
        proposal_mapping,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if (
        canonical_proposal != proposal_rendered
        or hashlib.sha256(proposal_rendered.encode("utf-8")).hexdigest() != decision.proposal_sha256
    ):
        raise ROIAnalysisError("transfer review proposal bytes are not canonical or exact")
    proposal = TransferReviewProposal.from_mapping(proposal_mapping)
    if (
        transfer_review_proposal_sha256(proposal) != decision.proposal_sha256
        or proposal.registration != attempt.registration
        or proposal.source_object_ids != attempt.source_object_ids
    ):
        raise ROIAnalysisError("transfer review decision proposal changed")
    expected_decision = issue_transfer_review_decision(
        proposal,
        action=decision.action,
        reviewed_by=decision.reviewed_by,
        accepted_interaction_response_sha256=(decision.accepted_interaction_response_sha256),
    )
    if decision != expected_decision:
        raise ROIAnalysisError("transfer review decision differs from the owner transition")
    return decision


def _transfer_attempt_reference(
    journal: _TransferAttemptJournal,
) -> dict[str, object]:
    """Project one durable attempt journal into bounded resume authority."""

    rendered = _secure_read_text(
        journal.attempt_path.parent,
        journal.attempt_path.name,
        expected_identity=journal.store_identity,
    ).encode("utf-8")
    if len(rendered) > 1_048_576:
        raise ROIAnalysisError("durable transfer attempt authority is unbounded")
    attempt = journal.attempt
    return {
        "schema_version": "warpy-transfer-attempt-reference/v1",
        "attempt_id": attempt.attempt_id,
        "path": str(journal.attempt_path),
        "sha256": hashlib.sha256(rendered).hexdigest(),
        "byte_count": len(rendered),
        "attempt_sha256": transfer_attempt_sha256(attempt),
        "qc_record_count": len(attempt.qc_records),
        "qc_records_sha256": attempt.qc_records_sha256,
    }


def _transfer_attempt_from_reference(project: object, value: object) -> TransferAttemptRecord:
    """Reopen the exact bounded attempt authority named by a resume request."""

    reference = _required_mapping(
        value,
        {
            "schema_version",
            "attempt_id",
            "path",
            "sha256",
            "byte_count",
            "attempt_sha256",
            "qc_record_count",
            "qc_records_sha256",
        },
        "transfer attempt reference",
    )
    if (
        set(reference)
        != {
            "schema_version",
            "attempt_id",
            "path",
            "sha256",
            "byte_count",
            "attempt_sha256",
            "qc_record_count",
            "qc_records_sha256",
        }
        or reference["schema_version"] != "warpy-transfer-attempt-reference/v1"
    ):
        raise ROIAnalysisError("transfer attempt reference fields changed")
    attempt_id = _validate_transfer_attempt_id(reference["attempt_id"])
    project_file = _absolute_without_links(getattr(project, "project_file"))
    directory = project_file.parent / "data" / "registration-transfer-attempts"
    expected_path = directory / f"{attempt_id}.json"
    if reference["path"] != str(expected_path):
        raise ROIAnalysisError("transfer attempt reference path scope changed")
    byte_count = reference["byte_count"]
    qc_count = reference["qc_record_count"]
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count <= 0
        or byte_count > 1_048_576
        or isinstance(qc_count, bool)
        or not isinstance(qc_count, int)
        or qc_count < 0
        or qc_count > 128
    ):
        raise ROIAnalysisError("transfer attempt reference bounds are invalid")
    store_identity = _secure_directory_identity(directory)
    rendered_text = _secure_read_text(
        directory,
        expected_path.name,
        expected_identity=store_identity,
    )
    rendered = rendered_text.encode("utf-8")
    if len(rendered) != byte_count or hashlib.sha256(rendered).hexdigest() != reference["sha256"]:
        raise ROIAnalysisError("durable transfer attempt reference digest changed")
    try:
        envelope = json.loads(rendered_text)
    except json.JSONDecodeError as exc:
        raise ROIAnalysisError("durable transfer attempt reference is invalid") from exc
    if (
        not isinstance(envelope, Mapping)
        or set(envelope)
        != {
            "schema_version",
            "attempt",
            "attempt_sha256",
            "operation_lock_identity",
        }
        or envelope.get("schema_version") != "warpy-transfer-attempt-envelope/v3"
    ):
        raise ROIAnalysisError("durable transfer attempt envelope fields changed")
    attempt = TransferAttemptRecord.from_mapping(envelope.get("attempt"))
    attempt_sha256 = transfer_attempt_sha256(attempt)
    if (
        attempt.attempt_id != attempt_id
        or attempt_sha256 != envelope.get("attempt_sha256")
        or attempt_sha256 != reference["attempt_sha256"]
        or len(attempt.qc_records) != qc_count
        or attempt.qc_records_sha256 != reference["qc_records_sha256"]
    ):
        raise ROIAnalysisError("durable transfer attempt authority changed")
    return attempt


@contextmanager
def _transfer_attempt_operation_lock(journal: _TransferAttemptJournal):
    """Serialize journal decisions with every exact external hierarchy mutation."""
    name = f"{journal.attempt.attempt_id}.operation.lock"
    backend = _windows_store_backend()
    if backend is not None:
        body_failure: BaseException | None = None
        try:
            with backend.open_directory(
                journal.events_dir.parent,
                expected_identity=_as_windows_identity(
                    journal.store_identity, "transfer attempt store identity"
                ),
            ) as directory:
                with backend.lock_regular_child(
                    directory,
                    name,
                    expected_identity=_as_windows_identity(
                        journal.operation_lock_identity,
                        "transfer operation lock identity",
                    ),
                ):
                    try:
                        yield
                    except BaseException as exc:
                        body_failure = exc
                        raise
            return
        except ROIAnalysisError as exc:
            if exc is body_failure:
                raise
            raise _OperationLockAuthorityFailure(journal, str(exc)) from exc
    authorities = journal.retained_lock_authorities
    if authorities is None:
        raise _WarpyContainmentManualReviewRequired(journal)
    authority = authorities.operation
    expected_path = journal.events_dir.parent / name
    if authority.path != expected_path or authority.identity != _as_posix_tuple(
        journal.operation_lock_identity, "transfer operation lock identity"
    ):
        raise _OperationLockAuthorityFailure(
            journal, "retained Warpy operation lock authority changed"
        )
    try:
        with authority.locked():
            yield
    except ROIAnalysisError as exc:
        raise _OperationLockAuthorityFailure(journal, str(exc)) from exc


def _hierarchy_lock_scope(
    attempt: TransferAttemptRecord, project_file: Path
) -> tuple[Mapping[str, str], str]:
    canonical_project = str(_absolute_without_links(project_file))
    fixed_entry_id = attempt.registration.fixed_project_entry_id
    if "\0" in canonical_project or "\0" in fixed_entry_id:
        raise ROIAnalysisError("transfer hierarchy mutation scope is invalid")
    scope = {
        "schema_version": "qupath-hierarchy-mutation-scope/v1",
        "project_file": canonical_project,
        "fixed_project_entry_id": fixed_entry_id,
    }
    digest = hashlib.sha256(
        "\0".join(
            (
                scope["schema_version"],
                canonical_project,
                fixed_entry_id,
            )
        ).encode("utf-8")
    ).hexdigest()
    return scope, digest


@contextmanager
def _transfer_hierarchy_lock(journal: _TransferAttemptJournal, lease: Mapping[str, Any]):
    """

    Holds the fixed-image annotation lock while QuPath writes accepted transferred objects.

    The current lease identifies the exact lock file. Windows acquires that
    file through the native file-lock functions; POSIX uses the retained open
    lock descriptor recorded for this attempt. The lock remains held until the
    caller leaves this context, including when its QuPath operation raises an error.

    Args:
        journal: Selected transfer attempt and its retained POSIX locks.
        lease: Current transfer lease naming the fixed-image lock and identity.

    Yields:
        Execution while the selected annotation hierarchy is exclusively locked.
    """

    if lease.get("schema_version") != "warpy-transfer-mutation-lease/v5":
        raise ROIAnalysisError("Use the current annotation-transfer request before acquiring its lock.")
    path = Path(lease["hierarchy_lock_path"])
    identity = file_identity_from_mapping(lease["hierarchy_lock_identity"])
    backend = _windows_store_backend()
    if backend is not None:
        with backend.open_directory(path.parent) as directory:
            with backend.lock_regular_child(
                directory, path.name,
                expected_identity=_as_windows_identity(identity, "transfer hierarchy lock identity"),
            ):
                yield
        return
    expected = _as_posix_tuple(identity, "transfer hierarchy lock identity")
    authorities = journal.retained_lock_authorities
    if authorities is None:
        raise _WarpyContainmentManualReviewRequired(journal)
    authority = authorities.hierarchies.get(path)
    if authority is None or authority.identity != expected:
        raise _WarpyContainmentManualReviewRequired(journal)
    with authority.locked():
        yield


def _transfer_lease_paths(journal: _TransferAttemptJournal) -> tuple[Path, Path]:
    base = journal.events_dir.parent
    attempt_id = journal.attempt.attempt_id
    _scope, scope_sha256 = _hierarchy_lock_scope(journal.attempt, journal.project_file)
    return (
        _absolute_without_links(base / f"{attempt_id}.lease.json"),
        (
            journal.project_file.parent / ".mif-warpy-hierarchy-locks" / f"{scope_sha256}.lock"
        ).absolute(),
    )


def _transfer_lease_payload(
    journal: _TransferAttemptJournal, *, operation: str
) -> dict[str, object]:
    assert_current_writer("warpy-transfer-mutation-lease", "5")
    if operation not in {"commit", "cleanup"}:
        raise ROIAnalysisError("transfer mutation lease operation is invalid")
    lease_head_path, hierarchy_lock_path = _transfer_lease_paths(journal)
    hierarchy_scope, hierarchy_scope_sha256 = _hierarchy_lock_scope(
        journal.attempt, journal.project_file
    )
    retained = journal.retained_lock_authorities
    retained_hierarchy = None if retained is None else retained.hierarchies.get(hierarchy_lock_path)
    if retained_hierarchy is not None:
        retained_hierarchy.validate()
        hierarchy_lock_identity: FileIdentity | tuple[int, int] = retained_hierarchy.identity
    else:
        hierarchy_lock_identity = _ensure_lock_path(hierarchy_lock_path)
        if retained is not None:
            retained.hierarchies[hierarchy_lock_path] = _retain_existing_posix_lock(
                hierarchy_lock_path,
                expected_identity=hierarchy_lock_identity,
            )
    lease: dict[str, object] = {
        "schema_version": "warpy-transfer-mutation-lease/v5",
        "attempt_id": journal.attempt.attempt_id,
        "epoch": journal.last_lease_epoch + 1,
        "token": uuid.uuid4().hex + uuid.uuid4().hex,
        "owner_id": f"pid-{os.getpid()}-{uuid.uuid4().hex}",
        "operation": operation,
        "lease_head_path": str(lease_head_path),
        "hierarchy_lock_path": str(hierarchy_lock_path),
        "hierarchy_lock_scope": hierarchy_scope,
        "hierarchy_lock_scope_sha256": hierarchy_scope_sha256,
        "attempt_store_identity": _identity_mapping(journal.store_identity, tagged=True),
        "hierarchy_lock_identity": _identity_mapping(hierarchy_lock_identity, tagged=True),
    }
    lease["lease_sha256"] = _transfer_lease_sha256(lease)
    return lease


def _validate_transfer_lease(
    attempt: TransferAttemptRecord, value: object, *, project_file: Path, events_dir: Path,
) -> Mapping[str, Any]:
    """

    Checks the current permission and file locks for one annotation-transfer write.

    We compare the attempt, operation, project and fixed-image entry before
    accepting the saved lease. Its file identities are checked against the
    existing attempt folder and annotation lock, and its checksum must reproduce
    the current field sequence used by both Python and QuPath.

    Args:
        attempt: Transfer attempt selected for commit or cleanup.
        value: Decoded version-5 transfer lease.
        project_file: QuPath project receiving the transferred annotations.
        events_dir: Saved event folder for this transfer attempt.

    Returns:
        Validated current lease as a plain dictionary.

    Raises:
        ROIAnalysisError: Format, fields, identifiers, paths, checksum or existing
            lock-file identities differ from the selected operation.
    """

    fields = {
        "schema_version", "attempt_id", "epoch", "token", "owner_id", "operation",
        "lease_head_path", "hierarchy_lock_path", "hierarchy_lock_scope",
        "hierarchy_lock_scope_sha256", "attempt_store_identity", "hierarchy_lock_identity", "lease_sha256",
    }
    lease = _required_mapping(value, fields, "annotation-transfer permission")
    if type(value) is not dict or set(lease) != fields or lease["schema_version"] != "warpy-transfer-mutation-lease/v5":
        raise ROIAnalysisError("The annotation-transfer permission uses an unsupported format.")
    assert_current_writer("warpy-transfer-mutation-lease", "5")
    text_fields = fields - {"epoch", "hierarchy_lock_scope", "attempt_store_identity", "hierarchy_lock_identity"}
    if any(type(key) is not str for key in lease) or any(type(lease[name]) is not str for name in text_fields):
        raise ROIAnalysisError("The annotation-transfer identifiers must be text.")
    if (
        lease["attempt_id"] != attempt.attempt_id or type(lease["epoch"]) is not int or lease["epoch"] < 1
        or not re.fullmatch("[0-9a-f]{64}", lease["token"]) or not lease["owner_id"].strip()
        or lease["operation"] not in {"commit", "cleanup"}
        or not Path(lease["lease_head_path"]).is_absolute() or not Path(lease["hierarchy_lock_path"]).is_absolute()
        or not re.fullmatch("[0-9a-f]{64}", lease["lease_sha256"])
    ):
        raise ROIAnalysisError("The annotation-transfer identifiers or file locations are invalid.")
    scope = lease["hierarchy_lock_scope"]
    if (
        type(scope) is not dict or set(scope) != {"schema_version", "project_file", "fixed_project_entry_id"}
        or any(type(key) is not str for key in scope) or any(type(item) is not str for item in scope.values())
    ):
        raise ROIAnalysisError("The annotation-transfer project and fixed-image entry are incomplete.")
    identities = []
    for name in ("attempt_store_identity", "hierarchy_lock_identity"):
        identity = lease[name]
        if type(identity) is not dict or any(type(key) is not str for key in identity):
            raise ROIAnalysisError("The annotation-transfer file identity is invalid.")
        if identity.get("schema_version") == "posix-file-identity/v1":
            if set(identity) != {"schema_version", "device", "inode"} or type(identity.get("device")) is not int or type(identity.get("inode")) is not int:
                raise ROIAnalysisError("The POSIX transfer lock needs its device and inode identifiers.")
        elif identity.get("schema_version") == "windows-file-identity/v1":
            if set(identity) != {"schema_version", "volume_serial_number", "file_id"} or type(identity.get("volume_serial_number")) is not str or type(identity.get("file_id")) is not str:
                raise ROIAnalysisError("The Windows transfer lock needs its volume and file identifiers.")
        else:
            raise ROIAnalysisError("The transfer lock uses an unsupported file-identity format.")
        identities.append(file_identity_from_mapping(identity))
    base = events_dir.parent
    _validate_transfer_attempt_id(attempt.attempt_id)
    expected_head = _absolute_without_links(base / f"{attempt.attempt_id}.lease.json")
    expected_scope, expected_scope_sha256 = _hierarchy_lock_scope(attempt, project_file)
    expected_lock = (_absolute_without_links(project_file).parent / ".mif-warpy-hierarchy-locks" / f"{expected_scope_sha256}.lock").absolute()
    if _mutable_json(scope) != _mutable_json(expected_scope) or lease["hierarchy_lock_scope_sha256"] != expected_scope_sha256:
        raise ROIAnalysisError("The annotation-transfer lock belongs to another project or fixed-image entry.")
    if lease["lease_sha256"] != _transfer_lease_sha256(lease):
        raise ROIAnalysisError("The annotation-transfer permission changed after it was recorded.")
    _secure_directory_identity(base, expected_identity=identities[0])
    _authenticate_existing_lock_path(expected_lock, expected_identity=identities[1])
    if lease["lease_head_path"] != str(expected_head) or lease["hierarchy_lock_path"] != str(expected_lock):
        raise ROIAnalysisError("The saved transfer-lock locations differ from this operation.")
    return _mutable_json(lease)


def _transfer_lease_release_payload(
    lease: Mapping[str, Any],
    *,
    status: str,
    post_mutation_evidence: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    if status not in {"completed", "failed", "stale_recovered"}:
        raise ROIAnalysisError("transfer mutation lease release status is invalid")
    if (
        status == "stale_recovered"
        and lease.get("schema_version") == "warpy-transfer-mutation-lease/v5"
    ):
        raise ROIAnalysisError("current Warpy stale recovery requires manual review")
    return {
        "schema_version": "warpy-transfer-mutation-lease-release/v1",
        "lease": _mutable_json(lease),
        "status": status,
        "post_mutation_evidence_sha256": (
            None if post_mutation_evidence is None else _json_sha256(post_mutation_evidence)
        ),
    }


def _validate_transfer_lease_release(
    attempt: TransferAttemptRecord,
    value: object,
    *,
    project_file: Path,
    events_dir: Path,
) -> Mapping[str, Any]:
    """Validate transfer lease release for the current operation.

    The required mapping shape includes ``schema_version``, ``lease``, ``status``,
    ``post_mutation_evidence_sha256``. Field checks are applied before the validated value
    is returned or passed to its next constructor."""

    fields = {
        "schema_version",
        "lease",
        "status",
        "post_mutation_evidence_sha256",
    }
    release = _required_mapping(value, fields, "transfer mutation lease release")
    digest = release["post_mutation_evidence_sha256"]
    if (
        set(release) != fields
        or release["schema_version"] != "warpy-transfer-mutation-lease-release/v1"
        or release["status"] not in {"completed", "failed", "stale_recovered"}
        or (
            digest is not None
            and (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            )
        )
    ):
        raise ROIAnalysisError("transfer mutation lease release scope changed")
    return {
        **_mutable_json(release),
        "lease": _validate_transfer_lease(
            attempt,
            release["lease"],
            project_file=project_file,
            events_dir=events_dir,
        ),
    }


def _write_transfer_lease_head(journal: _TransferAttemptJournal, lease: Mapping[str, Any], *, active: bool) -> None:
    """

    Records whether the current annotation-transfer write is active.

    We verify the fixed-image lock's existing file identity, then atomically
    replace the attempt's status file with the current lease and checksum. The
    attempt-folder identity is retained throughout the write.

    Args:
        journal: Transfer attempt supplying the saved status-file location.
        lease: Current permission to write the selected annotation hierarchy.
        active: Whether its QuPath write is currently active.
    """

    if lease.get("schema_version") != "warpy-transfer-mutation-lease/v5":
        raise ROIAnalysisError("The transfer status requires the current permission format.")
    lease_head_path, expected_hierarchy_path = _transfer_lease_paths(journal)
    if lease["lease_head_path"] != str(lease_head_path) or lease["hierarchy_lock_path"] != str(expected_hierarchy_path):
        raise ROIAnalysisError("The transfer status names a different annotation lock.")
    try:
        _ensure_lock_path(expected_hierarchy_path, expected_identity=file_identity_from_mapping(lease["hierarchy_lock_identity"]))
    except OSError as error:
        raise ROIAnalysisError("The annotation-transfer lock could not be opened safely.") from error
    value = {"schema_version": "warpy-transfer-mutation-lease-head/v1", "active": active,
        "lease": _mutable_json(lease), "lease_sha256": _transfer_lease_sha256(lease)}
    rendered = json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n"
    _secure_replace_text(lease_head_path.parent, lease_head_path.name, rendered, expected_identity=journal.store_identity)


def _acquire_transfer_lease(
    journal: _TransferAttemptJournal, *, operation: str
) -> tuple[_TransferAttemptJournal, Mapping[str, Any]]:
    current = _read_transfer_attempt_journal(
        journal.attempt,
        journal.project_file,
        journal.attempt_path,
        journal.events_dir,
        reused=True,
        store_identity=journal.store_identity,
        events_identity=journal.events_identity,
        operation_lock_identity=journal.operation_lock_identity,
        retained_lock_authorities=journal.retained_lock_authorities,
    )
    if current.active_lease is not None:
        raise ROIAnalysisError("transfer mutation lease is already active")
    lease = _transfer_lease_payload(current, operation=operation)
    current = _append_transfer_journal_event(current, "lease_acquired", lease)
    try:
        _write_transfer_lease_head(current, lease, active=True)
    except Exception:
        current = _append_transfer_journal_event(
            current,
            "lease_released",
            _transfer_lease_release_payload(lease, status="failed"),
        )
        raise
    return current, lease


def _release_transfer_lease(
    journal: _TransferAttemptJournal,
    lease: Mapping[str, Any],
    *,
    status: str,
    post_mutation_evidence: Mapping[str, Any] | None = None,
) -> _TransferAttemptJournal:
    current = journal
    try:
        current = _read_transfer_attempt_journal(
            journal.attempt,
            journal.project_file,
            journal.attempt_path,
            journal.events_dir,
            reused=True,
            store_identity=journal.store_identity,
            events_identity=journal.events_identity,
            operation_lock_identity=journal.operation_lock_identity,
            retained_lock_authorities=journal.retained_lock_authorities,
        )
        if current.containment_diagnostic_lease is not None or _mutable_json(
            current.active_lease
        ) != _mutable_json(lease):
            raise ROIAnalysisError("transfer mutation lease ownership was lost")
        _write_transfer_lease_head(current, lease, active=False)
        return _append_transfer_journal_event(
            current,
            "lease_released",
            _transfer_lease_release_payload(
                lease,
                status=status,
                post_mutation_evidence=post_mutation_evidence,
            ),
        )
    except Exception as exc:
        raise _LeaseReleaseFailure(current, exc) from exc


def _try_release_transfer_lease(
    journal: _TransferAttemptJournal,
    lease: Mapping[str, Any],
    *,
    status: str,
    post_mutation_evidence: Mapping[str, Any] | None = None,
) -> tuple[_TransferAttemptJournal, Exception | None]:
    """Retain the last validated journal when release publication is interrupted."""
    try:
        return (
            _release_transfer_lease(
                journal,
                lease,
                status=status,
                post_mutation_evidence=post_mutation_evidence,
            ),
            None,
        )
    except _LeaseReleaseFailure as exc:
        return exc.journal, exc.failure


def _recover_stale_transfer_lease(
    journal: _TransferAttemptJournal,
) -> _TransferAttemptJournal:
    """Refuse an active prior issuer; prototype recovery is manual-only."""
    current = journal
    try:
        current = _read_transfer_attempt_journal(
            journal.attempt,
            journal.project_file,
            journal.attempt_path,
            journal.events_dir,
            reused=True,
            store_identity=journal.store_identity,
            events_identity=journal.events_identity,
            operation_lock_identity=journal.operation_lock_identity,
            retained_lock_authorities=journal.retained_lock_authorities,
        )
        if current.containment_diagnostic_lease is not None:
            raise _WarpyContainmentManualReviewRequired(current)
        lease = current.active_lease
        if lease is None:
            return current
        raise _WarpyContainmentManualReviewRequired(current)
    except _WarpyContainmentManualReviewRequired:
        raise
    except _LeaseReleaseFailure:
        raise
    except Exception as exc:
        raise _LeaseReleaseFailure(current, exc) from exc
    return current


def _lease_request(lease: Mapping[str, Any]) -> Mapping[str, Any]:
    """

    Collects the current transfer permission fields sent to the QuPath write process.

    Args:
        lease: Validated current annotation-transfer lease.

    Returns:
        Complete request fields with copied nested project and file identities.
    """

    version = str(lease.get("schema_version", "")).removeprefix("warpy-transfer-mutation-lease/v")
    assert_current_writer("warpy-transfer-mutation-lease", version)
    fields = ("schema_version", "attempt_id", "epoch", "token", "owner_id", "operation", "lease_head_path",
        "hierarchy_lock_path", "hierarchy_lock_scope_sha256", "lease_sha256")
    return {**{name: lease[name] for name in fields},
        **{name: _mutable_json(lease[name]) for name in ("hierarchy_lock_scope", "attempt_store_identity", "hierarchy_lock_identity")}}


def _issue_warpy_containment_recorder(
    journal: _TransferAttemptJournal,
    lease: Mapping[str, Any],
    *,
    request_sha256: str,
    project_identity_sha256: str,
    mutation_run_sha256: str,
) -> WarpyContainedMutationJournal:
    """Issue one recorder only while this exact current lease owns the journal."""
    diagnostic = diagnose_warpy_containment_lease(lease.get("schema_version"))
    if not diagnostic.authorizes_containment_effects:
        raise ROIAnalysisError("historical Warpy containment requires manual review")
    if (
        lease.get("attempt_id") != journal.attempt.attempt_id
        or lease.get("operation") not in {"commit", "cleanup"}
        or lease.get("hierarchy_lock_scope_sha256") is None
        or lease.get("lease_sha256") != _transfer_lease_sha256(lease)
    ):
        raise ROIAnalysisError("Warpy containment recorder authority is invalid")

    def current_journal() -> _TransferAttemptJournal:
        return _read_transfer_attempt_journal(
            journal.attempt,
            journal.project_file,
            journal.attempt_path,
            journal.events_dir,
            reused=True,
            store_identity=journal.store_identity,
            events_identity=journal.events_identity,
            operation_lock_identity=journal.operation_lock_identity,
            retained_lock_authorities=journal.retained_lock_authorities,
        )

    def issuer_is_current() -> bool:
        current = current_journal()
        return current.containment_diagnostic_lease is None and _mutable_json(
            current.active_lease
        ) == _mutable_json(lease)

    def append_record(kind: str, payload: Mapping[str, object]) -> None:
        current = current_journal()
        if current.containment_diagnostic_lease is not None or _mutable_json(
            current.active_lease
        ) != _mutable_json(lease):
            raise ROIAnalysisError("Warpy containment issuer is foreign or stale")
        _append_transfer_journal_event(current, kind, payload)

    return WarpyContainedMutationJournal.issue(
        lease_schema_version=str(lease["schema_version"]),
        lease_sha256=str(lease["lease_sha256"]),
        attempt_id=journal.attempt.attempt_id,
        attempt_sha256=transfer_attempt_sha256(journal.attempt),
        operation=lease["operation"],
        request_sha256=request_sha256,
        project_identity_sha256=project_identity_sha256,
        mutation_run_sha256=mutation_run_sha256,
        hierarchy_scope_sha256=str(lease["hierarchy_lock_scope_sha256"]),
        append_record=append_record,
        issuer_is_current=issuer_is_current,
    )


def _persist_transfer_attempt(
    project: object,
    attempt: TransferAttemptRecord,
    *,
    retained_lock_authorities: _RetainedTransferLockAuthorities | None = None,
) -> _TransferAttemptJournal:
    project_file = _absolute_without_links(getattr(project, "project_file"))
    destination, events_dir = _transfer_attempt_store_paths(project, attempt)
    assert_current_writer("warpy-transfer-attempt-envelope", "3")
    envelope = {
        "schema_version": "warpy-transfer-attempt-envelope/v3",
        "attempt": attempt.to_mapping(),
        "attempt_sha256": transfer_attempt_sha256(attempt),
    }
    store_identity = _secure_directory_identity(destination.parent)
    try:
        prior = _secure_read_text(
            destination.parent,
            destination.name,
            expected_identity=store_identity,
        )
    except FileNotFoundError:
        prior = None
    reused = prior is not None
    newly_retained: _RetainedTransferLockAuthorities | None = None
    if prior is None:
        if retained_lock_authorities is not None:
            raise ROIAnalysisError("durable transfer attempt disappeared from retained authority")
        operation_lock_identity = _ensure_operation_lock(
            destination.parent,
            attempt.attempt_id,
            store_identity=store_identity,
        )
        if _windows_store_backend() is None:
            operation_path = destination.parent / (f"{attempt.attempt_id}.operation.lock")
            retained_lock_authorities = newly_retained = _RetainedTransferLockAuthorities(
                _RetainedTransferAuthorityScope(
                    project_file=project_file,
                    attempt_id=attempt.attempt_id,
                    operation_lock_identity=_as_posix_tuple(
                        operation_lock_identity,
                        "transfer operation lock identity",
                    ),
                ),
                _retain_existing_posix_lock(
                    operation_path,
                    expected_identity=operation_lock_identity,
                ),
            )
    else:
        operation_lock_identity = _stored_operation_lock_identity(prior, attempt)
        try:
            operation_path = destination.parent / (f"{attempt.attempt_id}.operation.lock")
            _authenticate_existing_lock_path(
                operation_path,
                expected_identity=operation_lock_identity,
            )
            if retained_lock_authorities is not None:
                retained_lock_authorities.validate_scope(
                    project_file=project_file,
                    attempt_id=attempt.attempt_id,
                    operation_lock_identity=operation_lock_identity,
                )
                retained_lock_authorities.operation.validate()
        except ROIAnalysisError as exc:
            events_identity = _secure_directory_identity(events_dir)
            journal = _read_transfer_attempt_journal(
                attempt,
                project_file,
                destination,
                events_dir,
                reused=True,
                store_identity=store_identity,
                events_identity=events_identity,
                operation_lock_identity=operation_lock_identity,
                retained_lock_authorities=retained_lock_authorities,
                authenticate_operation_lock=False,
            )
            raise _OperationLockAuthorityFailure(journal, str(exc)) from exc
    try:
        envelope["operation_lock_identity"] = _identity_mapping(
            operation_lock_identity, tagged=True
        )
        rendered = json.dumps(envelope, allow_nan=False, indent=2, sort_keys=True) + "\n"
        if prior is None:
            created = _secure_write_new(
                destination.parent,
                destination.name,
                rendered,
                expected_identity=store_identity,
            )
            reused = not created
            if (
                not created
                and _secure_read_text(
                    destination.parent,
                    destination.name,
                    expected_identity=store_identity,
                )
                != rendered
            ):
                raise ROIAnalysisError(
                    "append-only transfer attempt conflicts with concurrent bytes"
                )
        events_identity = _secure_directory_identity(events_dir, create=prior is None)
        return _read_transfer_attempt_journal(
            attempt,
            project_file,
            destination,
            events_dir,
            reused=reused,
            store_identity=store_identity,
            events_identity=events_identity,
            operation_lock_identity=operation_lock_identity,
            retained_lock_authorities=retained_lock_authorities,
        )
    except BaseException as exc:
        if newly_retained is not None:
            try:
                newly_retained.close()
            except BaseException as cleanup_exc:
                _add_bounded_diagnostic_note(
                    exc,
                    "unpublished Warpy authority cleanup also failed: ",
                    cleanup_exc,
                )
        raise


def _load_transfer_attempt_journal(
    project: object,
    attempt: TransferAttemptRecord,
    *,
    retained_lock_authorities: _RetainedTransferLockAuthorities | None = None,
) -> _TransferAttemptJournal:
    project_file = _absolute_without_links(getattr(project, "project_file"))
    destination, events_dir = _transfer_attempt_store_paths(project, attempt)
    store_identity = _secure_directory_identity(destination.parent)
    destination_identity = _secure_regular_child_identity(
        destination.parent,
        destination.name,
        expected_directory_identity=store_identity,
    )
    try:
        rendered_envelope = _secure_read_text(
            destination.parent,
            destination.name,
            expected_identity=store_identity,
        )
    except FileNotFoundError:
        rendered_envelope = None
    try:
        events_identity = _secure_directory_identity(events_dir)
    except FileNotFoundError:
        events_identity = None
    if destination_identity is None or rendered_envelope is None or events_identity is None:
        raise ROIAnalysisError("durable transfer attempt journal is missing")
    operation_lock_identity = _stored_operation_lock_identity(rendered_envelope, attempt)
    try:
        _authenticate_existing_lock_path(
            destination.parent / f"{attempt.attempt_id}.operation.lock",
            expected_identity=operation_lock_identity,
        )
        if retained_lock_authorities is not None:
            retained_lock_authorities.validate_scope(
                project_file=project_file,
                attempt_id=attempt.attempt_id,
                operation_lock_identity=operation_lock_identity,
            )
            retained_lock_authorities.operation.validate()
    except ROIAnalysisError as exc:
        journal = _read_transfer_attempt_journal(
            attempt,
            project_file,
            destination,
            events_dir,
            reused=True,
            store_identity=store_identity,
            events_identity=events_identity,
            operation_lock_identity=operation_lock_identity,
            retained_lock_authorities=retained_lock_authorities,
            authenticate_operation_lock=False,
        )
        raise _OperationLockAuthorityFailure(journal, str(exc)) from exc
    return _read_transfer_attempt_journal(
        attempt,
        project_file,
        destination,
        events_dir,
        reused=True,
        store_identity=store_identity,
        events_identity=events_identity,
        operation_lock_identity=operation_lock_identity,
        retained_lock_authorities=retained_lock_authorities,
    )


def _read_transfer_attempt_journal(
    attempt: TransferAttemptRecord,
    project_file: Path,
    attempt_path: Path,
    events_dir: Path,
    *,
    reused: bool,
    store_identity: FileIdentity | tuple[int, int] | None = None,
    events_identity: FileIdentity | tuple[int, int] | None = None,
    operation_lock_identity: FileIdentity | tuple[int, int] | None = None,
    retained_lock_authorities: _RetainedTransferLockAuthorities | None = None,
    authenticate_operation_lock: bool = True,
) -> _TransferAttemptJournal:
    if store_identity is None:
        store_identity = _secure_directory_identity(attempt_path.parent)
    if events_identity is None:
        events_identity = _secure_directory_identity(events_dir)
    if operation_lock_identity is None:
        rendered_for_identity = _secure_read_text(
            attempt_path.parent,
            attempt_path.name,
            expected_identity=store_identity,
        )
        operation_lock_identity = _stored_operation_lock_identity(rendered_for_identity, attempt)
    if authenticate_operation_lock:
        _authenticate_existing_lock_path(
            attempt_path.parent / f"{attempt.attempt_id}.operation.lock",
            expected_identity=operation_lock_identity,
        )
    if authenticate_operation_lock and retained_lock_authorities is not None:
        retained_lock_authorities.validate_scope(
            project_file=project_file,
            attempt_id=attempt.attempt_id,
            operation_lock_identity=operation_lock_identity,
        )
        retained_lock_authorities.operation.validate()
    try:
        rendered_envelope = _secure_read_text(
            attempt_path.parent,
            attempt_path.name,
            expected_identity=store_identity,
        )
        stored_operation_lock_identity = _stored_operation_lock_identity(rendered_envelope, attempt)
    except (OSError, json.JSONDecodeError) as exc:
        raise ROIAnalysisError("durable transfer attempt envelope is invalid") from exc
    normalized_stored_identity = (
        PosixFileIdentity(*stored_operation_lock_identity)
        if isinstance(stored_operation_lock_identity, tuple)
        else stored_operation_lock_identity
    )
    normalized_operation_lock_identity = (
        PosixFileIdentity(*operation_lock_identity)
        if isinstance(operation_lock_identity, tuple)
        else operation_lock_identity
    )
    if normalized_stored_identity != normalized_operation_lock_identity:
        raise ROIAnalysisError("durable transfer attempt envelope scope changed")

    entry_fields = {
        "schema_version",
        "attempt_id",
        "attempt_sha256",
        "sequence",
        "previous_entry_sha256",
        "event_type",
        "payload",
        "payload_sha256",
        "entry_sha256",
    }
    entries: list[Mapping[str, Any]] = []
    outcomes: list[Mapping[str, Any]] = []
    outcome_entry_sequences: list[int] = []
    cleanup_evidence: Mapping[str, Any] | None = None
    cleanup_entry_sequence: int | None = None
    active_lease: Mapping[str, Any] | None = None
    active_lease_entry_sequence: int | None = None
    containment_diagnostic_lease: Mapping[str, Any] | None = None
    active_post_mutation_evidence_sha256: str | None = None
    containment_context: Mapping[str, Any] | None = None
    containment_predecessor_sha256: str | None = None
    containment_predecessor: object | None = None
    containment_phase: str | None = None
    last_lease_epoch = 0
    previous_digest: str | None = None
    event_names = sorted(
        name
        for name in _secure_list_names(events_dir, expected_identity=events_identity)
        if name.endswith(".json")
    )
    for sequence, event_name in enumerate(event_names, 1):
        if event_name != f"{sequence:08d}.json":
            raise ROIAnalysisError("transfer attempt journal sequence is not contiguous")
        try:
            raw = json.loads(
                _secure_read_text(events_dir, event_name, expected_identity=events_identity)
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ROIAnalysisError("transfer attempt journal entry is invalid") from exc
        if not isinstance(raw, Mapping) or set(raw) != entry_fields:
            raise ROIAnalysisError("transfer attempt journal entry fields changed")
        payload = raw["payload"]
        body = {key: raw[key] for key in entry_fields - {"entry_sha256"}}
        if (
            raw["schema_version"] != "warpy-transfer-attempt-journal-entry/v1"
            or raw["attempt_id"] != attempt.attempt_id
            or raw["attempt_sha256"] != transfer_attempt_sha256(attempt)
            or raw["sequence"] != sequence
            or raw["previous_entry_sha256"] != previous_digest
            or raw["event_type"]
            not in {
                "commit_outcome",
                "cleanup_attestation",
                "lease_acquired",
                "lease_released",
                "process_spawn_attempt",
                "process_launch",
                "process_drain",
            }
            or raw["payload_sha256"] != _json_sha256(payload)
            or raw["entry_sha256"] != _json_sha256(body)
        ):
            raise ROIAnalysisError("transfer attempt journal digest or scope changed")
        previous_digest = raw["entry_sha256"]
        entry = _mutable_json(raw)
        entries.append(entry)
        if raw["event_type"] in {"process_spawn_attempt", "process_launch", "process_drain"}:
            if active_lease is None or not isinstance(payload, Mapping):
                raise ROIAnalysisError("Warpy containment record lacks one active lease")
            kind = str(raw["event_type"])
            if containment_context is None:
                if kind != "process_spawn_attempt":
                    raise ROIAnalysisError("Warpy containment journal phase is out of order")
                containment_context = {
                    name: payload.get(name)
                    for name in (
                        "request_sha256",
                        "project_identity_sha256",
                        "mutation_run_sha256",
                    )
                }
            expected_phase = {
                "process_spawn_attempt": None,
                "process_launch": "process_spawn_attempt",
                "process_drain": containment_phase,
            }[kind]
            if (kind != "process_drain" and containment_phase != expected_phase) or (
                kind == "process_drain"
                and containment_phase not in {"process_spawn_attempt", "process_launch"}
            ):
                raise ROIAnalysisError("Warpy containment journal phase is out of order")
            record = validate_warpy_containment_record(
                kind,
                payload,
                lease_sha256=str(active_lease["lease_sha256"]),
                attempt_id=attempt.attempt_id,
                attempt_sha256=transfer_attempt_sha256(attempt),
                operation=active_lease["operation"],
                request_sha256=containment_context["request_sha256"],
                project_identity_sha256=containment_context["project_identity_sha256"],
                mutation_run_sha256=containment_context["mutation_run_sha256"],
                hierarchy_scope_sha256=active_lease["hierarchy_lock_scope_sha256"],
                predecessor_record_sha256=containment_predecessor_sha256,
            )
            if kind == "process_launch" and (
                not hasattr(record, "spawn_attempt")
                or record.spawn_attempt != containment_predecessor
            ):
                raise ROIAnalysisError("Warpy containment launch predecessor changed")
            if kind == "process_drain" and (
                not hasattr(record, "launch_or_attempt")
                or record.launch_or_attempt != containment_predecessor
            ):
                raise ROIAnalysisError("Warpy containment drain predecessor changed")
            containment_predecessor = record
            containment_predecessor_sha256 = payload["record_sha256"]
            containment_phase = kind
        elif raw["event_type"] == "commit_outcome":
            if not isinstance(payload, Mapping):
                raise ROIAnalysisError("transfer attempt journal outcome is invalid")
            outcomes.append(_mutable_json(payload))
            outcome_entry_sequences.append(sequence)
            if (
                active_lease is not None
                and payload.get("status") in {"committed", "reconciled"}
                and isinstance(payload.get("live_attestation"), Mapping)
            ):
                active_post_mutation_evidence_sha256 = _json_sha256(payload["live_attestation"])
        elif raw["event_type"] == "cleanup_attestation":
            if cleanup_evidence is not None or not isinstance(payload, Mapping):
                raise ROIAnalysisError("transfer attempt journal cleanup attestation is duplicated")
            cleanup_evidence = _validate_transfer_cleanup_evidence(attempt, payload)
            cleanup_entry_sequence = sequence
            if active_lease is not None:
                active_post_mutation_evidence_sha256 = _json_sha256(cleanup_evidence)
        elif raw["event_type"] == "lease_acquired":
            lease = _validate_transfer_lease(
                attempt,
                payload,
                project_file=project_file,
                events_dir=events_dir,
            )
            if active_lease is not None or lease["epoch"] != last_lease_epoch + 1:
                raise ROIAnalysisError("transfer mutation lease epoch fork detected")
            active_lease = lease
            if not diagnose_warpy_containment_lease(
                lease["schema_version"]
            ).authorizes_containment_effects:
                containment_diagnostic_lease = lease
            active_lease_entry_sequence = sequence
            active_post_mutation_evidence_sha256 = None
            last_lease_epoch = lease["epoch"]
            containment_context = None
            containment_predecessor_sha256 = None
            containment_predecessor = None
            containment_phase = None
        else:
            release = _validate_transfer_lease_release(
                attempt,
                payload,
                project_file=project_file,
                events_dir=events_dir,
            )
            if active_lease is None or _mutable_json(release["lease"]) != _mutable_json(
                active_lease
            ):
                raise ROIAnalysisError("transfer mutation lease release fork detected")
            if (
                active_lease["schema_version"] == "warpy-transfer-mutation-lease/v5"
                and release["status"] == "stale_recovered"
            ):
                containment_diagnostic_lease = active_lease
                continue
            if (
                active_lease["schema_version"] == "warpy-transfer-mutation-lease/v5"
                and (release["status"] == "completed" or containment_phase == "process_launch")
                and containment_phase != "process_drain"
            ):
                raise ROIAnalysisError("current Warpy containment lacks terminal drain evidence")
            evidence_digest = release["post_mutation_evidence_sha256"]
            if (
                (
                    release["status"] == "completed"
                    and (
                        evidence_digest != active_post_mutation_evidence_sha256
                        or (active_lease["operation"] == "commit" and evidence_digest is None)
                    )
                )
                or (release["status"] == "stale_recovered" and evidence_digest is not None)
                or (
                    release["status"] == "failed"
                    and evidence_digest not in {None, active_post_mutation_evidence_sha256}
                )
            ):
                raise ROIAnalysisError("transfer mutation lease post-mutation evidence changed")
            active_lease = None
            active_lease_entry_sequence = None
            active_post_mutation_evidence_sha256 = None
            containment_context = None
            containment_predecessor_sha256 = None
            containment_predecessor = None
            containment_phase = None

    validated_outcomes = _validate_commit_outcomes(outcomes, attempt)
    committed_indexes = [
        index for index, item in enumerate(validated_outcomes) if item["status"] == "committed"
    ]
    committed_entry_sequence = (
        None if not committed_indexes else outcome_entry_sequences[committed_indexes[0]]
    )
    terminal = None if not validated_outcomes else validated_outcomes[-1]
    cleanup_required = bool(
        terminal is not None
        and terminal["status"] in {"quarantined", "aborted"}
        and (
            committed_entry_sequence is not None
            or any(
                item["status"] == "failed"
                and item["failure"]["mutation_state"] == "indeterminate_or_persisted"
                for item in validated_outcomes
            )
        )
    )
    if terminal is not None and terminal["status"] in {"quarantined", "aborted"}:
        if cleanup_required != (cleanup_evidence is not None):
            raise ROIAnalysisError("durable noncommit outcome lacks exact cleanup attestation")
    if cleanup_evidence is not None:
        risk_sequences = [
            outcome_entry_sequences[index]
            for index, item in enumerate(validated_outcomes)
            if item["status"] == "committed"
            or (
                item["status"] == "failed"
                and item["failure"]["mutation_state"] == "indeterminate_or_persisted"
            )
        ]
        if risk_sequences and cleanup_entry_sequence <= max(risk_sequences):
            raise ROIAnalysisError("transfer cleanup attestation predates persistence risk")
    commit_evidence = (
        None
        if not committed_indexes
        else validated_outcomes[committed_indexes[0]]["live_attestation"]
    )
    if (
        active_lease is not None
        and active_lease["schema_version"] == "warpy-transfer-mutation-lease/v5"
        and _windows_store_backend() is None
    ):
        hierarchy_path = Path(active_lease["hierarchy_lock_path"])
        retained_hierarchy = (
            None
            if retained_lock_authorities is None
            else retained_lock_authorities.hierarchies.get(hierarchy_path)
        )
        if retained_hierarchy is None:
            containment_diagnostic_lease = active_lease
        else:
            retained_hierarchy.validate()
    return _TransferAttemptJournal(
        attempt=attempt,
        project_file=project_file,
        attempt_path=attempt_path,
        events_dir=events_dir,
        reused=reused,
        entries=tuple(entries),
        outcomes=validated_outcomes,
        commit_evidence=commit_evidence,
        cleanup_evidence=cleanup_evidence,
        committed_entry_sequence=committed_entry_sequence,
        cleanup_entry_sequence=cleanup_entry_sequence,
        active_lease=active_lease,
        active_lease_entry_sequence=active_lease_entry_sequence,
        last_lease_epoch=last_lease_epoch,
        containment_diagnostic_lease=containment_diagnostic_lease,
        store_identity=store_identity,
        events_identity=events_identity,
        operation_lock_identity=operation_lock_identity,
        retained_lock_authorities=retained_lock_authorities,
    )


def _append_transfer_journal_outcome(
    journal: _TransferAttemptJournal, outcome: Mapping[str, Any]
) -> _TransferAttemptJournal:
    current = _read_transfer_attempt_journal(
        journal.attempt,
        journal.project_file,
        journal.attempt_path,
        journal.events_dir,
        reused=True,
        store_identity=journal.store_identity,
        events_identity=journal.events_identity,
        operation_lock_identity=journal.operation_lock_identity,
        retained_lock_authorities=journal.retained_lock_authorities,
    )
    _validate_commit_outcomes((*current.outcomes, outcome), current.attempt)
    return _append_transfer_journal_event(current, "commit_outcome", outcome)


def _append_transfer_journal_cleanup(
    journal: _TransferAttemptJournal, evidence: Mapping[str, Any]
) -> _TransferAttemptJournal:
    current = _read_transfer_attempt_journal(
        journal.attempt,
        journal.project_file,
        journal.attempt_path,
        journal.events_dir,
        reused=True,
        store_identity=journal.store_identity,
        events_identity=journal.events_identity,
        operation_lock_identity=journal.operation_lock_identity,
        retained_lock_authorities=journal.retained_lock_authorities,
    )
    validated = _validate_transfer_cleanup_evidence(current.attempt, evidence)
    if current.cleanup_evidence is not None:
        if _mutable_json(current.cleanup_evidence) == _mutable_json(validated):
            return current
        raise ROIAnalysisError("durable transfer cleanup attestation conflicts")
    return _append_transfer_journal_event(current, "cleanup_attestation", validated)


def _append_transfer_journal_event(
    journal: _TransferAttemptJournal,
    event_type: str,
    payload: Mapping[str, Any],
) -> _TransferAttemptJournal:
    sequence = len(journal.entries) + 1
    mutable_payload = _mutable_json(payload)
    body = {
        "schema_version": "warpy-transfer-attempt-journal-entry/v1",
        "attempt_id": journal.attempt.attempt_id,
        "attempt_sha256": transfer_attempt_sha256(journal.attempt),
        "sequence": sequence,
        "previous_entry_sha256": (
            None if not journal.entries else journal.entries[-1]["entry_sha256"]
        ),
        "event_type": event_type,
        "payload": mutable_payload,
        "payload_sha256": _json_sha256(mutable_payload),
    }
    entry = {**body, "entry_sha256": _json_sha256(body)}
    rendered = json.dumps(entry, allow_nan=False, indent=2, sort_keys=True) + "\n"
    destination_name = f"{sequence:08d}.json"
    created = _secure_write_new(
        journal.events_dir,
        destination_name,
        rendered,
        expected_identity=journal.events_identity,
    )
    if (
        not created
        and _secure_read_text(
            journal.events_dir,
            destination_name,
            expected_identity=journal.events_identity,
        )
        != rendered
    ):
        raise ROIAnalysisError("transfer attempt journal terminal outcome fork detected")
    return _read_transfer_attempt_journal(
        journal.attempt,
        journal.project_file,
        journal.attempt_path,
        journal.events_dir,
        reused=True,
        store_identity=journal.store_identity,
        events_identity=journal.events_identity,
        operation_lock_identity=journal.operation_lock_identity,
        retained_lock_authorities=journal.retained_lock_authorities,
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            _mutable_json(value), allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _warpy_project_identity_sha256(project_file: object) -> str:
    return _json_sha256(
        {
            "schema_version": "qupath-project-file-identity/v1",
            "project_file": str(Path(project_file).resolve()),
        }
    )


def _transfer_lease_sha256(lease: Mapping[str, Any]) -> str:
    """

    Calculates the checksum shared by Python and QuPath for a current transfer permission.

    We encode the lease, project, fixed-image entry and two platform-specific
    file identities in a fixed field order. A null separator makes adjacent
    text fields distinguishable; the final byte sequence is hashed with SHA-256.

    Args:
        lease: Version-5 transfer permission containing its project and lock identities.

    Returns:
        Lowercase hexadecimal checksum of the complete ordered field sequence.

    Raises:
        ROIAnalysisError: The format is unsupported or a field contains a null character.
    """

    if lease.get("schema_version") != "warpy-transfer-mutation-lease/v5":
        raise ROIAnalysisError("The transfer checksum requires the current permission format.")
    scope = lease["hierarchy_lock_scope"]
    values = [lease["schema_version"], lease["attempt_id"], str(lease["epoch"]), lease["token"], lease["owner_id"],
        lease["operation"], lease["lease_head_path"], lease["hierarchy_lock_path"], scope["schema_version"],
        scope["project_file"], scope["fixed_project_entry_id"], lease["hierarchy_lock_scope_sha256"]]
    for name in ("attempt_store_identity", "hierarchy_lock_identity"):
        parsed = file_identity_from_mapping(lease[name])
        encoded = parsed.to_mapping()
        values.append(lease[name]["schema_version"])
        fields = ("device", "inode") if isinstance(parsed, PosixFileIdentity) else ("volume_serial_number", "file_id")
        values.extend(str(encoded[field]) for field in fields)
    if any(not isinstance(item, str) or "\0" in item for item in values):
        raise ROIAnalysisError("The transfer checksum contains an invalid text field.")
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


def _validate_transfer_commit_evidence(
    attempt: TransferAttemptRecord, value: object
) -> Mapping[str, Any]:
    evidence = _required_mapping(
        value,
        {
            "schema_version",
            "attempt_id",
            "registration_id",
            "registration_revision",
            "authoritative_object_ids",
            "objects",
        },
        "durable transfer commit evidence",
    )
    accepted = tuple(item for item in attempt.qc_records if item.authoritative)
    expected_ids = [item.target_object_id for item in accepted]
    if (
        set(evidence)
        != {
            "schema_version",
            "attempt_id",
            "registration_id",
            "registration_revision",
            "authoritative_object_ids",
            "objects",
        }
        or evidence["schema_version"] != "qupath-warpy-transfer-commit/v1"
        or evidence["attempt_id"] != attempt.attempt_id
        or evidence["registration_id"] != attempt.registration.registration_id
        or evidence["registration_revision"] != attempt.registration.revision
        or list(evidence["authoritative_object_ids"]) != expected_ids
    ):
        raise ROIAnalysisError("durable transfer commit evidence scope changed")
    objects = evidence["objects"]
    if isinstance(objects, (str, bytes)) or not isinstance(objects, Sequence):
        raise ROIAnalysisError("durable transfer commit rows are invalid")
    expected_rows = [
        {
            "source_object_id": item.source_object_id,
            "target_object_id": item.target_object_id,
            "target_section_id": item.target_section_id,
            "target_geometry_sha256": item.transformed_geometry_sha256,
        }
        for item in accepted
    ]
    if len(objects) != len(expected_rows):
        raise ROIAnalysisError("durable transfer commit rows are incomplete")
    for expected, raw in zip(expected_rows, objects, strict=True):
        row = _required_mapping(raw, {*expected, "reconciled"}, "durable transfer commit row")
        if (
            set(row) != {*expected, "reconciled"}
            or not isinstance(row["reconciled"], bool)
            or any(row[name] != expected[name] for name in expected)
        ):
            raise ROIAnalysisError("durable transfer commit row scope changed")
    return _mutable_json(evidence)


def _validate_transfer_cleanup_evidence(
    attempt: TransferAttemptRecord, value: object
) -> Mapping[str, Any]:
    """Validate transfer cleanup evidence for the current operation.

    The required mapping shape includes ``schema_version``, ``attempt_id``,
    ``registration_id``, ``registration_revision``, ``absent_target_object_ids``,
    ``objects``. Field checks are applied before the validated value is returned or passed
    to its next constructor."""

    evidence = _required_mapping(
        value,
        {
            "schema_version",
            "attempt_id",
            "registration_id",
            "registration_revision",
            "absent_target_object_ids",
            "objects",
        },
        "durable transfer cleanup evidence",
    )
    fields = {
        "schema_version",
        "attempt_id",
        "registration_id",
        "registration_revision",
        "absent_target_object_ids",
        "objects",
    }
    accepted = tuple(item for item in attempt.qc_records if item.authoritative)
    expected_ids = [item.target_object_id for item in accepted]
    if (
        set(evidence) != fields
        or evidence["schema_version"] != "qupath-warpy-transfer-cleanup/v1"
        or evidence["attempt_id"] != attempt.attempt_id
        or evidence["registration_id"] != attempt.registration.registration_id
        or evidence["registration_revision"] != attempt.registration.revision
        or list(evidence["absent_target_object_ids"]) != expected_ids
    ):
        raise ROIAnalysisError("Warpy transfer cleanup scope changed")
    objects = evidence["objects"]
    if isinstance(objects, (str, bytes)) or not isinstance(objects, Sequence):
        raise ROIAnalysisError("Warpy transfer cleanup rows are invalid")
    expected_rows = [
        {
            "source_object_id": item.source_object_id,
            "target_object_id": item.target_object_id,
            "transfer_qc_id": item.transfer_qc_id,
            "absent": True,
        }
        for item in accepted
    ]
    if list(objects) != expected_rows:
        raise ROIAnalysisError("Warpy transfer cleanup absence attestation changed")
    return _mutable_json(evidence)


def _transfer_commit_failure(
    attempt: TransferAttemptRecord,
    failure: Exception,
    *,
    cleanup_required: bool,
) -> dict[str, str]:
    return {
        "schema_version": "warpy-transfer-commit-failure/v1",
        "attempt_id": attempt.attempt_id,
        "exception_type": type(failure).__name__,
        "message": str(failure),
        "mutation_state": (
            "indeterminate_or_persisted" if cleanup_required else "proven_pre_mutation"
        ),
    }


def _validate_checkpointed_outcome_summary(
    context: Mapping[str, Any],
    durable: Sequence[Mapping[str, Any]],
) -> None:
    count = context.get("commit_outcome_count")
    statuses = context.get("commit_outcome_statuses")
    digest = context.get("commit_outcomes_sha256")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or count > len(durable)
        or isinstance(statuses, (str, bytes))
        or not isinstance(statuses, Sequence)
        or list(statuses) != [str(item["status"]) for item in durable[:count]]
        or digest != _json_sha256(list(durable[:count]))
    ):
        raise ROIAnalysisError("checkpointed transfer outcome summary differs from durable journal")


def _completed_transfer_attempt_result(
    config: AnalysisConfig,
    generation: TissueSectionGeneration,
    attempt: TransferAttemptRecord,
    fixed: Mapping[str, Any],
    moving: Mapping[str, Any],
    journal: _TransferAttemptJournal,
    *,
    decision_reference: Mapping[str, object] | None = None,
) -> StageResult:
    if (
        attempt.schema_version == "warpy-transfer-attempt/v1"
        and any(item.authoritative for item in attempt.qc_records)
        and decision_reference is None
    ):
        raise ROIAnalysisError("authoritative transfer completion requires a decision reference")
    outputs: dict[str, object] = {
        "mode": "warpy",
        "registration": attempt.registration.to_mapping(),
        "moving_reference": dict(moving),
        "fixed_reference": dict(fixed),
        "transfer_qc_records": [item.to_mapping() for item in attempt.qc_records],
        "transfer_attempts": [attempt.to_mapping()],
        "transfer_commit_evidence": _mutable_json(journal.commit_evidence),
        "transfer_commit_outcomes": [_mutable_json(item) for item in journal.outcomes],
        "transfer_cleanup_evidence": _mutable_json(journal.cleanup_evidence),
    }
    if decision_reference is not None:
        outputs["transfer_review_decision_reference"] = dict(decision_reference)
    return StageResult.completed(
        stage="registration",
        slide_id=config.slide_id,
        generation_id=generation.generation_id,
        affected_object_ids=tuple(item.section_id for item in generation.sections),
        outputs=outputs,
    )


def _transfer_commit_outcome(
    attempt: TransferAttemptRecord,
    *,
    sequence: int,
    status: str,
    failure: Mapping[str, Any] | None = None,
    live_attestation: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    failure_mapping = None if failure is None else _mutable_json(failure)
    failure_sha256 = (
        None
        if failure_mapping is None
        else hashlib.sha256(
            json.dumps(failure_mapping, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    return {
        "schema_version": "warpy-transfer-commit-outcome/v1",
        "attempt_id": attempt.attempt_id,
        "sequence": sequence,
        "status": status,
        "failure": failure_mapping,
        "failure_sha256": failure_sha256,
        "live_attestation": (None if live_attestation is None else _mutable_json(live_attestation)),
    }


def _validate_commit_outcomes(
    value: object, attempt: TransferAttemptRecord
) -> tuple[Mapping[str, Any], ...]:
    """Validate commit outcomes for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ROIAnalysisError("checkpointed transfer commit outcomes are invalid")
    outcomes: list[Mapping[str, Any]] = []
    authoritative = any(item.authoritative for item in attempt.qc_records)
    for sequence, raw in enumerate(value, 1):
        mapping = _required_mapping(
            raw,
            {
                "schema_version",
                "attempt_id",
                "sequence",
                "status",
                "failure",
                "failure_sha256",
                "live_attestation",
            },
            "transfer commit outcome",
        )
        if (
            set(mapping)
            != {
                "schema_version",
                "attempt_id",
                "sequence",
                "status",
                "failure",
                "failure_sha256",
                "live_attestation",
            }
            or mapping["schema_version"] != "warpy-transfer-commit-outcome/v1"
            or mapping["attempt_id"] != attempt.attempt_id
            or mapping["sequence"] != sequence
            or mapping["status"]
            not in {"failed", "committed", "reconciled", "not_required", "quarantined", "aborted"}
        ):
            raise ROIAnalysisError("checkpointed transfer commit outcome scope changed")
        status = mapping["status"]
        if status in {"committed", "reconciled"}:
            if (
                not authoritative
                or mapping["failure"] is not None
                or mapping["failure_sha256"] is not None
            ):
                raise ROIAnalysisError("live transfer outcome scope changed")
            live = _validate_transfer_commit_evidence(attempt, mapping["live_attestation"])
            mapping = {**mapping, "live_attestation": live}
        elif status == "not_required":
            if (
                authoritative
                or mapping["failure"] is not None
                or mapping["failure_sha256"] is not None
                or mapping["live_attestation"] is not None
            ):
                raise ROIAnalysisError("not-required transfer outcome scope changed")
        else:
            if mapping["live_attestation"] is not None:
                raise ROIAnalysisError("noncommit transfer outcome has live evidence")
            failure = _required_mapping(
                mapping["failure"],
                {
                    "schema_version",
                    "attempt_id",
                    "exception_type",
                    "message",
                    "mutation_state",
                },
                "transfer commit outcome failure",
            )
            if (
                set(failure)
                != {
                    "schema_version",
                    "attempt_id",
                    "exception_type",
                    "message",
                    "mutation_state",
                }
                or failure["schema_version"] != "warpy-transfer-commit-failure/v1"
                or failure["attempt_id"] != attempt.attempt_id
                or failure["mutation_state"]
                not in {"proven_pre_mutation", "indeterminate_or_persisted"}
            ):
                raise ROIAnalysisError("transfer commit outcome failure scope changed")
            digest = _json_sha256(failure)
            if mapping["failure_sha256"] != digest:
                raise ROIAnalysisError("checkpointed transfer commit outcome digest mismatch")
        outcomes.append(_mutable_json(mapping))

    statuses = [item["status"] for item in outcomes]
    committed_indexes = [index for index, status in enumerate(statuses) if status == "committed"]
    terminal_noncommit_indexes = [
        index
        for index, status in enumerate(statuses)
        if status in {"not_required", "quarantined", "aborted"}
    ]
    if len(committed_indexes) > 1 or len(terminal_noncommit_indexes) > 1:
        raise ROIAnalysisError("transfer attempt journal has terminal outcome fork")
    if terminal_noncommit_indexes and terminal_noncommit_indexes[0] != len(statuses) - 1:
        raise ROIAnalysisError("transfer attempt journal continues after terminal outcome")
    if "not_required" in statuses and len(statuses) != 1:
        raise ROIAnalysisError("not-required transfer outcome history is invalid")
    live_state = False
    reconciliation_failed = False
    for index, status in enumerate(statuses):
        if status == "failed":
            if live_state:
                reconciliation_failed = True
            continue
        if status == "committed":
            if live_state or reconciliation_failed:
                raise ROIAnalysisError("committed transfer outcome history forked")
            live_state = True
            continue
        if status == "reconciled":
            if not live_state:
                raise ROIAnalysisError("reconciled transfer outcome predecessor is invalid")
            reconciliation_failed = False
            continue
        if status == "not_required":
            if index != 0 or len(statuses) != 1:
                raise ROIAnalysisError("not-required transfer outcome history is invalid")
            continue
        if status in {"quarantined", "aborted"} and index != len(statuses) - 1:
            raise ROIAnalysisError("transfer attempt journal continues after terminal outcome")
    return tuple(outcomes)


def _append_transfer_failure_best_effort(
    journal: _TransferAttemptJournal,
    attempt: TransferAttemptRecord,
    failure: Exception,
    *,
    cleanup_required: bool,
) -> _TransferAttemptJournal:
    """Publish one lock-owned recovery failure, retaining validated state on I/O loss."""
    failure_mapping = _transfer_commit_failure(attempt, failure, cleanup_required=cleanup_required)
    terminal_status = None if not journal.outcomes else journal.outcomes[-1]["status"]
    already_recorded = bool(
        journal.outcomes
        and journal.outcomes[-1]["status"] == "failed"
        and _mutable_json(journal.outcomes[-1]["failure"]) == _mutable_json(failure_mapping)
    )
    if terminal_status in {"not_required", "quarantined", "aborted"} or already_recorded:
        return journal
    try:
        return _append_transfer_journal_outcome(
            journal,
            _transfer_commit_outcome(
                attempt,
                sequence=len(journal.outcomes) + 1,
                status="failed",
                failure=failure_mapping,
            ),
        )
    except OSError:
        try:
            return _read_transfer_attempt_journal(
                journal.attempt,
                journal.project_file,
                journal.attempt_path,
                journal.events_dir,
                reused=True,
                store_identity=journal.store_identity,
                events_identity=journal.events_identity,
                operation_lock_identity=journal.operation_lock_identity,
                retained_lock_authorities=journal.retained_lock_authorities,
            )
        except OSError:
            return journal


def _transfer_commit_recovery_result(
    config: AnalysisConfig,
    generation: TissueSectionGeneration,
    attempt: TransferAttemptRecord,
    failure: Exception,
    fixed: Mapping[str, Any],
    moving: Mapping[str, Any],
    *,
    journal: _TransferAttemptJournal,
    cleanup_required: bool,
    publish_failure: bool = False,
    decision_reference: Mapping[str, object] | None = None,
) -> StageResult:
    if (
        attempt.schema_version == "warpy-transfer-attempt/v1"
        and any(item.authoritative for item in attempt.qc_records)
        and decision_reference is None
    ):
        raise ROIAnalysisError("authoritative transfer recovery requires a decision reference")
    failure_mapping = _transfer_commit_failure(attempt, failure, cleanup_required=cleanup_required)
    failure_sha256 = hashlib.sha256(
        json.dumps(failure_mapping, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if publish_failure:
        journal = _append_transfer_failure_best_effort(
            journal,
            attempt,
            failure,
            cleanup_required=cleanup_required,
        )
    resume_context: dict[str, object] = {
        "schema_version": "transfer-commit-recovery-interaction/v1",
        "transfer_attempt_reference": _transfer_attempt_reference(journal),
        "transfer_attempt_sha256": transfer_attempt_sha256(attempt),
        "commit_failure": failure_mapping,
        "commit_failure_sha256": failure_sha256,
        "commit_outcome_count": len(journal.outcomes),
        "commit_outcome_statuses": [str(item["status"]) for item in journal.outcomes],
        "commit_outcomes_sha256": _json_sha256(list(journal.outcomes)),
        "project_file": fixed["project_file"],
        "moving_project_entry_id": moving["project_entry_id"],
        "fixed_project_entry_id": fixed["project_entry_id"],
    }
    if decision_reference is not None:
        resume_context["transfer_review_decision_reference"] = dict(decision_reference)
    return StageResult.needs_interaction(
        stage="registration",
        slide_id=config.slide_id,
        generation_id=generation.generation_id,
        reason=InteractionReason.TRANSFER_COMMIT_RECOVERY_REQUIRED,
        affected_object_ids=tuple(item.source_object_id for item in attempt.qc_records),
        message=(
            "Transfer QC is preserved, but authoritative commit/final attestation failed. "
            "Retry exact reconciliation, quarantine the attempt, or abort it."
        ),
        allowed_actions=(
            "retry_transfer_commit",
            "quarantine_transfer_attempt",
            "abort_transfer_attempt",
        ),
        resume_context=resume_context,
        checkpoint_id="pending-workflow-checkpoint",
    )


def _warpy_containment_manual_review_result(
    config: AnalysisConfig,
    generation: TissueSectionGeneration,
    journal: _TransferAttemptJournal,
    fixed: Mapping[str, Any],
    moving: Mapping[str, Any],
    *,
    decision_reference: Mapping[str, object] | None = None,
) -> StageResult:
    if (
        journal.attempt.schema_version == "warpy-transfer-attempt/v1"
        and any(item.authoritative for item in journal.attempt.qc_records)
        and decision_reference is None
    ):
        raise ROIAnalysisError("authoritative transfer manual review requires a decision reference")
    lease = journal.containment_diagnostic_lease or journal.active_lease
    if lease is None:
        lease_schema_version = "warpy-transfer-mutation-lease/v5"
        lease_record_sha256 = _json_sha256(
            {
                "attempt_id": journal.attempt.attempt_id,
                "operation_lock_identity": _identity_mapping(
                    journal.operation_lock_identity, tagged=True
                ),
                "retained_current_capability": False,
            }
        )
    else:
        diagnostic = diagnose_warpy_containment_lease(lease.get("schema_version"))
        lease_schema_version = diagnostic.schema_version
        lease_record_sha256 = _json_sha256(lease)
    resume_context: dict[str, object] = {
        "schema_version": "warpy-containment-manual-review-interaction/v1",
        "transfer_attempt_reference": _transfer_attempt_reference(journal),
        "transfer_attempt_sha256": transfer_attempt_sha256(journal.attempt),
        "lease_schema_version": lease_schema_version,
        "lease_record_sha256": lease_record_sha256,
        "containment_effects_authorized": False,
        "project_file": fixed["project_file"],
        "moving_project_entry_id": moving["project_entry_id"],
        "fixed_project_entry_id": fixed["project_entry_id"],
    }
    if decision_reference is not None:
        resume_context["transfer_review_decision_reference"] = dict(decision_reference)
    return StageResult.needs_interaction(
        stage="registration",
        slide_id=config.slide_id,
        generation_id=generation.generation_id,
        reason=InteractionReason.OTHER_EXPLICIT_REASON,
        affected_object_ids=tuple(item.source_object_id for item in journal.attempt.qc_records),
        message=(
            "Existing Warpy process-containment evidence is diagnostic only. "
            "Do not reuse, recover, fence, or launch it; review the project manually."
        ),
        allowed_actions=("abort",),
        resume_context=resume_context,
        checkpoint_id="pending-workflow-checkpoint",
    )


def _registration_import_request(
    invocation: object,
    generation: TissueSectionGeneration,
    fixed: Mapping[str, Any],
    moving: Mapping[str, Any],
    moving_sha256: str,
    transform_file: Path,
) -> dict[str, Any]:
    """Build the existing exact-entry request without invoking native work."""
    action = getattr(invocation, "action", None)
    action_context = getattr(invocation, "action_context", {})
    if not isinstance(action_context, Mapping):
        raise ROIAnalysisError("registration action context must be a mapping")
    transform_sha256 = _file_sha256(transform_file)
    pending_record, pending_digest, _, _ = _pending_registration(action_context)
    if action in {"accept_registration", "accept_registration_and_transfer", "reject"}:
        if pending_record is None or pending_digest is None:
            raise ROIAnalysisError(
                "registration review action requires the exact checkpointed pending record"
            )
    revision = pending_record.revision if pending_record is not None else 1
    registration_id = _registration_id(
        generation,
        str(moving["project_entry_id"]),
        str(fixed["project_entry_id"]),
        moving_sha256,
        transform_sha256,
        revision,
    )
    section_scope = [
        {
            "section_id": item.section_id,
            "tissue_roi_id": item.roi_id,
            "tissue_path_object_id": item.path_object_id,
            "generation_id": generation.generation_id,
            "generation_status": "accepted",
            "review_status": "accepted",
            "pipeline_stage": "tissue_detection",
        }
        for item in generation.sections
    ]
    return {
        "schema_version": "qupath-warpy-registration-request/v2",
        "transform_reader": transform_reader_request(),
        "registration_id": registration_id,
        "project_file": fixed["project_file"],
        "moving_project_entry_id": moving["project_entry_id"],
        "fixed_project_entry_id": fixed["project_entry_id"],
        "moving_image_id": moving["image_id"],
        "fixed_image_id": fixed["image_id"],
        "moving_image_path": moving["image_path"],
        "fixed_image_path": fixed["image_path"],
        "moving_image_sha256": moving_sha256,
        "fixed_image_sha256": generation.canonical_image_sha256,
        "transform_file": str(transform_file.resolve()),
        "transform_file_sha256": transform_sha256,
        "coordinate_pre_transform": None,
        "coordinate_post_transform": None,
        "accepted_tissue_sections": section_scope,
        "sample_spacing_px": 128.0,
    }


def _pending_registration(
    action_context: Mapping[str, Any],
) -> tuple[
    RegistrationRecord | None,
    str | None,
    RegistrationRecord | None,
    str | None,
]:
    context = resolve_resume_envelope(action_context).service_context
    raw = context.get("pending_registration")
    digest = context.get("pending_registration_sha256")
    if raw is None and digest is None:
        return None, None, None, None
    record = RegistrationRecord.from_mapping(raw)
    if not isinstance(digest, str) or registration_record_sha256(record) != digest:
        raise ROIAnalysisError("checkpointed pending registration digest mismatch")
    if record.acceptance_state != "pending_review":
        raise ROIAnalysisError("checkpointed registration is not pending review")
    predecessor_raw = context.get("predecessor_registration")
    predecessor_digest = context.get("predecessor_registration_sha256")
    if record.revision == 1:
        if predecessor_raw is not None or predecessor_digest is not None:
            raise ROIAnalysisError("initial pending registration cannot carry a predecessor")
        return record, digest, None, None
    predecessor = RegistrationRecord.from_mapping(predecessor_raw)
    if (
        not isinstance(predecessor_digest, str)
        or registration_record_sha256(predecessor) != predecessor_digest
    ):
        raise ROIAnalysisError("checkpointed registration predecessor digest mismatch")
    return record, digest, predecessor, predecessor_digest


def _lineage_scope(record: RegistrationRecord) -> tuple[object, ...]:
    return (
        record.provider,
        record.provider_version,
        record.moving_project_entry_id,
        record.fixed_project_entry_id,
        record.moving_image_id,
        record.fixed_image_id,
        record.moving_image_sha256,
        record.fixed_image_sha256,
        record.moving_image_path,
        record.fixed_image_path,
        record.direction,
        record.moving_physical_calibration,
        record.fixed_physical_calibration,
        record.coordinate_pre_transform,
        record.coordinate_post_transform,
        record.transform_file,
        record.moving_modality,
        record.moving_image_metadata,
        record.tissue_generation_id,
        record.tissue_section_ids,
        record.tissue_roi_ids,
        record.tissue_path_object_ids,
    )


def _revise_pending_record(
    record: RegistrationRecord, *, revision: int, supersedes: str, predecessor_sha256: str
) -> RegistrationRecord:
    mapping = record.to_mapping()
    registration_id = _registration_id_from_record(record, revision)
    mapping.update(
        {
            "registration_id": registration_id,
            "revision": revision,
            "supersedes_registration_id": supersedes,
            "predecessor_record_sha256": predecessor_sha256,
        }
    )
    qc = dict(mapping["registration_qc"])
    qc["registration_id"] = registration_id
    qc["qc_id"] = f"registration-qc-{registration_id.removeprefix('registration-')}"
    mapping["registration_qc"] = qc
    return RegistrationRecord.from_mapping(mapping)


def _registration_id_from_record(record: RegistrationRecord, revision: int) -> str:
    payload = {
        "provider": "warpy",
        "generation": record.tissue_generation_id,
        "sections": list(record.tissue_section_ids),
        "moving_entry": record.moving_project_entry_id,
        "fixed_entry": record.fixed_project_entry_id,
        "moving_sha256": record.moving_image_sha256,
        "fixed_sha256": record.fixed_image_sha256,
        "transform_sha256": record.transform_file_sha256,
        "revision": revision,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"registration-{digest[:24]}"


def _registration_review_result(
    config: AnalysisConfig,
    generation: TissueSectionGeneration,
    record: RegistrationRecord,
    fixed: Mapping[str, Any],
    moving: Mapping[str, Any],
    *,
    predecessor: RegistrationRecord | None = None,
    predecessor_sha256: str | None = None,
    allow_object_transfer: bool = True,
) -> StageResult:
    resume_context: dict[str, object] = {
        "schema_version": "registration-review-interaction/v1",
        "pending_registration": record.to_mapping(),
        "pending_registration_sha256": registration_record_sha256(record),
        "project_file": fixed["project_file"],
        "moving_project_entry_id": moving["project_entry_id"],
        "fixed_project_entry_id": fixed["project_entry_id"],
        "transform_file_sha256": record.transform_file_sha256,
        "revision": record.revision,
    }
    if predecessor is not None:
        if predecessor_sha256 is None:
            raise ROIAnalysisError("registration predecessor digest is required")
        resume_context.update(
            {
                "predecessor_registration": predecessor.to_mapping(),
                "predecessor_registration_sha256": predecessor_sha256,
            }
        )
    return StageResult.needs_interaction(
        stage="registration",
        slide_id=config.slide_id,
        generation_id=generation.generation_id,
        reason=InteractionReason.REGISTRATION_REVIEW_REQUIRED,
        affected_object_ids=tuple(item.section_id for item in generation.sections),
        message="Review the alignment checks for the image and each tissue region.",
        allowed_actions=(
            (
                "accept_registration",
                "accept_registration_and_transfer",
                "reject",
                "side_by_side",
                "skip_moving_reference",
            )
            if allow_object_transfer
            else (
                "accept_registration",
                "reject",
                "side_by_side",
                "skip_moving_reference",
            )
        ),
        resume_context=resume_context,
        checkpoint_id="pending-workflow-checkpoint",
    )


def _transfer_review_result(
    config: AnalysisConfig,
    generation: TissueSectionGeneration,
    fixed: Mapping[str, Any],
    moving: Mapping[str, Any],
    reference: Mapping[str, object],
) -> StageResult:
    context = {
        "schema_version": "transfer-review-interaction/v1",
        "transfer_review_proposal_reference": dict(reference),
        "proposal_sha256": reference["sha256"],
        "project_file": fixed["project_file"],
        "moving_project_entry_id": moving["project_entry_id"],
        "fixed_project_entry_id": fixed["project_entry_id"],
        "registration_sha256": reference["registration_sha256"],
        "source_object_count": reference["source_object_count"],
        "qc_record_count": reference["qc_record_count"],
    }
    return StageResult.needs_interaction(
        stage="registration",
        slide_id=config.slide_id,
        generation_id=generation.generation_id,
        reason=InteractionReason.TRANSFER_REVIEW_REQUIRED,
        affected_object_ids=(),
        message=(
            "Review the exact pending Warpy transfer-QC proposal before any "
            "objects are added to the fixed hierarchy."
        ),
        allowed_actions=("accept_transfers", "reject_transfers"),
        resume_context=context,
        checkpoint_id=str(reference["sha256"]),
    )


def _resolved_transfer_review_response(
    value: object,
) -> tuple[str, str, str, Mapping[str, Any]]:
    response = _required_mapping(
        value,
        {
            "action",
            "reviewed_by",
            "accepted_response_sha256",
            "interaction_context",
        },
        "resolved transfer review response",
    )
    action = response["action"]
    if action not in {"accept_transfers", "reject_transfers"}:
        raise ROIAnalysisError("resolved transfer review action is unsupported")
    reviewed_by = response["reviewed_by"]
    if (
        not isinstance(reviewed_by, str)
        or not reviewed_by.strip()
        or reviewed_by != reviewed_by.strip()
    ):
        raise ROIAnalysisError("resolved transfer review reviewer is invalid")
    response_sha256 = response["accepted_response_sha256"]
    if (
        not isinstance(response_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", response_sha256) is None
    ):
        raise ROIAnalysisError("resolved transfer review response digest is invalid")
    validated = response["interaction_context"]
    if not isinstance(validated, Mapping):
        raise ROIAnalysisError("resolved transfer review context is invalid")
    context_fields = {
        "schema_version",
        "transfer_review_proposal_reference",
        "proposal_sha256",
        "project_file",
        "moving_project_entry_id",
        "fixed_project_entry_id",
        "registration_sha256",
        "source_object_count",
        "qc_record_count",
    }
    if set(validated) != context_fields:
        raise ROIAnalysisError(
            "resolved transfer review context fields do not match the owner schema"
        )
    return str(action), reviewed_by, response_sha256, dict(validated)


def _effective_registration(
    config: AnalysisConfig,
    state: Mapping[str, Any],
) -> tuple[str, Path | None, str | None, Path | None, str, Mapping[str, Any]]:
    choice = state.get("_registration_choice")
    if isinstance(choice, Mapping) and isinstance(choice.get("mode"), str):
        moving = choice.get("moving_image")
        transform = choice.get("transform_file")
        return (
            choice["mode"],
            None if moving is None else Path(moving).expanduser().resolve(),
            choice.get("moving_image_id"),
            None if transform is None else Path(transform).expanduser().resolve(),
            choice.get("moving_modality", config.registration.moving_modality),
            choice.get("source_metadata", config.registration.moving_image_metadata),
        )
    registration = config.registration
    return (
        registration.mode,
        registration.moving_image,
        registration.moving_image_id,
        registration.transform_file,
        registration.moving_modality,
        registration.moving_image_metadata,
    )


def _fallback_audit(state: Mapping[str, Any]) -> dict[str, object]:
    choice = state.get("_registration_choice")
    if not isinstance(choice, Mapping) or "fallback_registration_attempt" not in choice:
        return {
            "registration_attempt": None,
            "registration_attempt_sha256": None,
            "fallback_decision": None,
        }
    record = RegistrationRecord.from_mapping(choice.get("fallback_registration_attempt"))
    digest = choice.get("fallback_registration_attempt_sha256")
    if not isinstance(digest, str) or registration_record_sha256(record) != digest:
        raise ROIAnalysisError("fallback registration attempt digest mismatch")
    decision = choice.get("fallback_decision")
    if not isinstance(decision, Mapping):
        raise ROIAnalysisError("fallback registration decision is invalid")
    return {
        "registration_attempt": record.to_mapping(),
        "registration_attempt_sha256": digest,
        "fallback_decision": _mutable_json(decision),
    }


def _required_mapping(value: object, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not fields.issubset(value):
        raise ROIAnalysisError(f"{label} is incomplete")
    return value


def _registration_id(
    generation: TissueSectionGeneration,
    moving_entry: str,
    fixed_entry: str,
    moving_sha256: str,
    transform_sha256: str,
    revision: object,
) -> str:
    payload = {
        "provider": "warpy",
        "generation": generation.generation_id,
        "sections": [item.section_id for item in generation.sections],
        "moving_entry": moving_entry,
        "fixed_entry": fixed_entry,
        "moving_sha256": moving_sha256,
        "fixed_sha256": generation.canonical_image_sha256,
        "transform_sha256": transform_sha256,
        "revision": revision,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"registration-{digest[:24]}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_registration_files(record: RegistrationRecord) -> None:
    """Recheck each image's identity member and the exact saved transform file."""
    for path, expected, label in (
        (record.moving_image_path, record.moving_image_sha256, "moving image"),
        (record.fixed_image_path, record.fixed_image_sha256, "fixed image"),
        (record.transform_file, record.transform_file_sha256, "transform"),
    ):
        try:
            member = path if label == "transform" else image_identity_member(path)
        except (OSError, ValueError) as exc:
            raise ROIAnalysisError(f"registration {label} scope changed") from exc
        if not member.is_file() or _file_sha256(member) != expected:
            raise ROIAnalysisError(f"registration {label} scope changed")


def _mutable_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(item) for item in value]
    return value


def _failed(
    config: AnalysisConfig,
    generation: TissueSectionGeneration | None,
    code: str,
    message: str,
    **details: object,
) -> StageResult:
    return StageResult.failed(
        stage="registration",
        slide_id=config.slide_id,
        generation_id=None if generation is None else generation.generation_id,
        error_code=code,
        error_message=message,
        error_context=details,
    )


__all__ = ["ProductionRegistrationExecutor"]
