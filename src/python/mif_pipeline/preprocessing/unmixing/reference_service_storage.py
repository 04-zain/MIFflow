"""Immutable local generations for spectral-reference workflow checkpoints.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Callable, Iterator

from .publication_lock import _AuthorityPublicationLock
from .reference_models import ReferenceInputEvidence
from .reference_service_start_binding import (
    _MAX_START_AUTHORITY_BYTES,
    _parse_start_authority,
    _start_authority_bytes,
)
from .reference_store_fs import (
    ReferenceCheckpointConflictError,
    ReferenceCheckpointStoreError,
    _DirectoryIdentity,
    _PinnedDirectoryHandle,
    _install_immutable,
    _prepare_root,
    _prepare_run_root,
    _read_bounded_regular,
    _replace_current_pointer,
    _validate_sha256,
)
from .reference_workflow import (
    ReferenceWorkflowCheckpoint,
    dumps_reference_workflow_checkpoint,
    loads_reference_workflow_checkpoint,
)

_POINTER_SCHEMA_NAME = "mif_pipeline.spectral_reference_checkpoint_pointer"
_POINTER_SCHEMA_VERSION = 1
_MAX_POINTER_BYTES = 16_384
_MAX_GENERATION_BYTES = 2_097_152


# ==============================================================================
#                           Reference service storage
# ==============================================================================


def _strict_json_object(raw: bytes, label: str) -> dict[str, object]:
    def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ReferenceCheckpointStoreError(f"duplicate {label} JSON field: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ReferenceCheckpointStoreError(f"nonfinite {label} JSON value is forbidden: {value}")

    try:
        value = json.loads(
            raw,
            object_pairs_hook=exact_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ReferenceCheckpointStoreError(f"{label} is not strict JSON") from exc
    if type(value) is not dict:
        raise ReferenceCheckpointStoreError(f"{label} must be a JSON object")
    return value


def _generation_name(checkpoint: ReferenceWorkflowCheckpoint) -> str:
    return f"{checkpoint.revision}-{checkpoint.content_sha256}.json"


def _pointer_bytes(checkpoint: ReferenceWorkflowCheckpoint, run_sha256: str) -> bytes:
    return json.dumps(
        {
            "schema_name": _POINTER_SCHEMA_NAME,
            "schema_version": _POINTER_SCHEMA_VERSION,
            "run_sha256": run_sha256,
            "revision": checkpoint.revision,
            "checkpoint_sha256": checkpoint.content_sha256,
            "generation": _generation_name(checkpoint),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _parse_pointer(raw: bytes, run_sha256: str) -> tuple[int, str, str]:
    value = _strict_json_object(raw, "checkpoint current pointer")
    expected = {
        "schema_name",
        "schema_version",
        "run_sha256",
        "revision",
        "checkpoint_sha256",
        "generation",
    }
    if set(value) != expected:
        raise ReferenceCheckpointStoreError("checkpoint current pointer fields are invalid")
    if (value["schema_name"], value["schema_version"]) != (
        _POINTER_SCHEMA_NAME,
        _POINTER_SCHEMA_VERSION,
    ):
        raise ReferenceCheckpointStoreError("checkpoint current pointer schema is invalid")
    if value["run_sha256"] != run_sha256:
        raise ReferenceCheckpointStoreError("checkpoint current pointer crosses run identity")
    revision = value["revision"]
    if type(revision) is not int or revision < 0:
        raise ReferenceCheckpointStoreError("checkpoint pointer revision is invalid")
    checkpoint_sha256 = _validate_sha256(value["checkpoint_sha256"], "checkpoint pointer SHA-256")
    generation = value["generation"]
    expected_generation = f"{revision}-{checkpoint_sha256}.json"
    if generation != expected_generation:
        raise ReferenceCheckpointStoreError("checkpoint pointer generation is invalid")
    return revision, checkpoint_sha256, expected_generation


_GENERATION_NAME = re.compile(r"[0-9]+-[0-9a-f]{64}\.json\Z")
_START_AUTHORITY_NAME = re.compile(r"start-authority-[0-9a-f]{64}\.json\Z")


def _strict_named_inventory(run_root: Path, pattern: re.Pattern[str]) -> tuple[Path, ...]:
    try:
        names = tuple(item.name for item in os.scandir(run_root))
    except OSError as exc:
        raise ReferenceCheckpointStoreError("checkpoint run inventory cannot be inspected") from exc
    return tuple(
        run_root / name for name in sorted(name for name in names if pattern.fullmatch(name))
    )


def _strict_orphan_inventory(
    run_root: Path,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    try:
        names = tuple(item.name for item in os.scandir(run_root))
    except OSError as exc:
        raise ReferenceCheckpointStoreError(
            "checkpoint orphan inventory cannot be inspected"
        ) from exc
    generations: list[Path] = []
    starts: list[Path] = []
    for name in names:
        if name == ".current.json.publication.lock":
            continue
        if _GENERATION_NAME.fullmatch(name):
            generations.append(run_root / name)
        elif _START_AUTHORITY_NAME.fullmatch(name):
            starts.append(run_root / name)
        else:
            raise ReferenceCheckpointStoreError(
                "missing current pointer has an unrecognized orphan entry"
            )
    return tuple(sorted(generations)), tuple(sorted(starts))


@dataclass(frozen=True, slots=True)
class _LockedRun:
    run_sha256: str
    run_root: Path
    current_path: Path
    publication_lock: _AuthorityPublicationLock
    directory_handle: _PinnedDirectoryHandle
    guard: Callable[[], None]


class ReferenceCheckpointStore:
    """One immutable checkpoint lineage per hashed workflow run ID."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = _prepare_root(output_root)
        self._output_identity = _DirectoryIdentity.capture(
            self.output_root, "checkpoint output root"
        )

    @staticmethod
    def _run_sha256(run_id: str) -> str:
        if type(run_id) is not str or not run_id or run_id != run_id.strip():
            raise ValueError("workflow run ID must be nonempty literal text")
        return hashlib.sha256(run_id.encode("utf-8")).hexdigest()

    def _validate_output_location(self) -> None:
        self._output_identity.revalidate("checkpoint output root", exact_link_count=False)

    @contextmanager
    def _locked_run(self, run_id: str, *, create: bool) -> Iterator[_LockedRun]:
        run_sha256 = self._run_sha256(run_id)
        self._validate_output_location()
        namespace_authority = self.output_root / ".reference-checkpoint-namespace"
        output_expected = _DirectoryIdentity.capture(self.output_root, "checkpoint output root")
        with (
            _PinnedDirectoryHandle(
                self.output_root,
                "checkpoint output root",
                expected=output_expected,
            ) as output_handle,
            _AuthorityPublicationLock(namespace_authority) as namespace_lock,
        ):
            self._validate_output_location()
            if namespace_lock.authority_path.parent != self._output_identity.canonical_path:
                raise ReferenceCheckpointStoreError(
                    "checkpoint namespace lock escaped the pinned output root"
                )
            output_pin = _DirectoryIdentity.capture(self.output_root, "checkpoint output root")

            def guard_output() -> None:
                output_pin.revalidate("checkpoint output root")
                output_handle.validate()
                namespace_lock.validate_identity()
                if namespace_lock.authority_path.parent != output_pin.canonical_path:
                    raise ReferenceCheckpointStoreError("checkpoint namespace lock parent changed")

            run_root = _prepare_run_root(
                self.output_root,
                run_sha256,
                create=create,
                output_handle=output_handle,
                before_mutation=guard_output,
            )
            if create:
                output_pin = output_handle.refresh_after_owned_child_directory_change()
            run_pin = _DirectoryIdentity.capture(run_root, "checkpoint run directory")
            current_path = run_root / "current.json"
            with (
                _PinnedDirectoryHandle(
                    run_root,
                    "checkpoint run directory",
                    expected=run_pin,
                ) as run_handle,
                _AuthorityPublicationLock(current_path) as run_lock,
            ):
                if run_lock.authority_path.parent != run_pin.canonical_path:
                    raise ReferenceCheckpointStoreError(
                        "checkpoint publication lock escaped the pinned run root"
                    )

                def guard() -> None:
                    output_pin.revalidate("checkpoint output root")
                    run_pin.revalidate("checkpoint run directory")
                    output_handle.validate()
                    run_handle.validate()
                    namespace_lock.validate_identity()
                    run_lock.validate_identity()
                    if (
                        namespace_lock.authority_path.parent != output_pin.canonical_path
                        or run_lock.authority_path.parent != run_pin.canonical_path
                    ):
                        raise ReferenceCheckpointStoreError(
                            "checkpoint lock parent differs from pinned authority"
                        )

                guard()
                yield _LockedRun(
                    run_sha256,
                    run_root,
                    current_path,
                    run_lock,
                    run_handle,
                    guard,
                )
                guard()

    def _read_paths(self, run_id: str) -> tuple[str, Path, Path, _DirectoryIdentity]:
        run_sha256 = self._run_sha256(run_id)
        self._validate_output_location()
        run_root = _prepare_run_root(self.output_root, run_sha256, create=False)
        run_pin = _DirectoryIdentity.capture(run_root, "checkpoint run directory")
        return run_sha256, run_root, run_root / "current.json", run_pin

    def _load_from_paths(
        self, run_id: str, run_sha256: str, run_root: Path, current_path: Path
    ) -> ReferenceWorkflowCheckpoint:
        pointer_raw = _read_bounded_regular(
            current_path,
            limit=_MAX_POINTER_BYTES,
            label="checkpoint current pointer",
        )
        revision, checkpoint_sha256, generation_name = _parse_pointer(pointer_raw, run_sha256)
        generation_path = run_root / generation_name
        if generation_path.parent != run_root:
            raise ReferenceCheckpointStoreError("checkpoint generation escaped run root")
        raw = _read_bounded_regular(
            generation_path,
            limit=_MAX_GENERATION_BYTES,
            label="checkpoint generation",
        )
        try:
            checkpoint = loads_reference_workflow_checkpoint(raw)
        except (TypeError, ValueError) as exc:
            raise ReferenceCheckpointStoreError(
                "checkpoint generation does not contain a valid workflow checkpoint"
            ) from exc
        if (
            checkpoint.run_id != run_id
            or checkpoint.revision != revision
            or checkpoint.content_sha256 != checkpoint_sha256
            or _generation_name(checkpoint) != generation_name
        ):
            raise ReferenceCheckpointStoreError(
                "checkpoint generation differs from current pointer authority"
            )
        canonical = dumps_reference_workflow_checkpoint(checkpoint).encode("utf-8")
        if canonical != raw:
            raise ReferenceCheckpointStoreError("checkpoint generation bytes are not canonical")
        return checkpoint

    def load(self, run_id: str) -> ReferenceWorkflowCheckpoint:
        run_sha256, run_root, current_path, run_pin = self._read_paths(run_id)
        checkpoint = self._load_from_paths(run_id, run_sha256, run_root, current_path)
        self._validate_output_location()
        run_pin.revalidate("checkpoint run directory")
        return checkpoint

    def _load_start_authority(
        self,
        run_sha256: str,
        run_root: Path,
        checkpoint: ReferenceWorkflowCheckpoint,
    ) -> str:
        inventory = _strict_named_inventory(run_root, _START_AUTHORITY_NAME)
        if len(inventory) != 1:
            raise ReferenceCheckpointStoreError(
                "reference service requires exactly one start authority binding"
            )
        raw = _read_bounded_regular(
            inventory[0],
            limit=_MAX_START_AUTHORITY_BYTES,
            label="reference start authority",
        )
        expected_name_sha256 = inventory[0].stem.removeprefix("start-authority-")
        content_sha256 = _parse_start_authority(raw, run_sha256=run_sha256, checkpoint=checkpoint)
        if content_sha256 != expected_name_sha256:
            raise ReferenceCheckpointStoreError(
                "reference start authority filename differs from its content"
            )
        return content_sha256

    def _load_service_checkpoint(self, run_id: str) -> tuple[ReferenceWorkflowCheckpoint, str]:
        run_sha256, run_root, current_path, run_pin = self._read_paths(run_id)
        checkpoint = self._load_from_paths(run_id, run_sha256, run_root, current_path)
        start_sha256 = self._load_start_authority(run_sha256, run_root, checkpoint)
        self._validate_output_location()
        run_pin.revalidate("checkpoint run directory")
        return checkpoint, start_sha256

    @staticmethod
    def _load_generation_path(run_id: str, path: Path) -> ReferenceWorkflowCheckpoint:
        raw = _read_bounded_regular(
            path,
            limit=_MAX_GENERATION_BYTES,
            label="checkpoint generation",
        )
        try:
            checkpoint = loads_reference_workflow_checkpoint(raw)
        except (TypeError, ValueError) as exc:
            raise ReferenceCheckpointStoreError(
                "checkpoint generation does not contain a valid workflow checkpoint"
            ) from exc
        if (
            checkpoint.run_id != run_id
            or path.name != _generation_name(checkpoint)
            or dumps_reference_workflow_checkpoint(checkpoint).encode("utf-8") != raw
        ):
            raise ReferenceCheckpointStoreError("checkpoint generation bytes or identity differ")
        return checkpoint

    def _load_service_decision_authority(
        self,
        run_id: str,
        *,
        source_checkpoint_sha256: str,
        decision_record_sha256: str,
    ) -> tuple[ReferenceWorkflowCheckpoint, ReferenceWorkflowCheckpoint, str]:
        """Reopen the exact persisted source/result pair for one decision."""

        source_sha256 = _validate_sha256(
            source_checkpoint_sha256, "decision source checkpoint SHA-256"
        )
        decision_sha256 = _validate_sha256(decision_record_sha256, "decision record SHA-256")
        with self._locked_run(run_id, create=False) as locked:
            current = self._load_from_paths(
                run_id,
                locked.run_sha256,
                locked.run_root,
                locked.current_path,
            )
            start_sha256 = self._load_start_authority(locked.run_sha256, locked.run_root, current)
            matches = [(index, transition) for index, transition in enumerate(current.transition_log)
                if transition.source_checkpoint_sha256 == source_sha256
                and transition.decision_record_sha256 == decision_sha256]
            if len(matches) != 1:
                raise ReferenceCheckpointStoreError(
                    "decision source/result checkpoint authority is missing or ambiguous"
                )
            index, transition = matches[0]
            # The validated live transition chain already names both immutable
            # generations. Do not reparse every unrelated historical checkpoint.
            result_sha256 = (current.content_sha256 if index + 1 == len(current.transition_log)
                             else current.transition_log[index + 1].source_checkpoint_sha256)
            source = self._load_generation_path(run_id,
                locked.run_root / f'{transition.revision - 1}-{source_sha256}.json')
            result = self._load_generation_path(run_id,
                locked.run_root / f'{transition.revision}-{result_sha256}.json')
            if (source.content_sha256 != source_sha256 or result.content_sha256 != result_sha256
                    or result.prior_checkpoint_sha256 != source_sha256 or not result.decisions
                    or result.decisions[-1].content_sha256 != decision_sha256
                    or result.decisions[-1].source_checkpoint_sha256 != source_sha256
                    or source.transition_log != current.transition_log[:index]
                    or result.transition_log != current.transition_log[:index + 1]
                    or source._authoritative_state_mapping() != json.loads(transition.source_state_json)
                    or result._authoritative_state_mapping() != json.loads(transition.target_state_json)):
                raise ReferenceCheckpointStoreError('Decision generations differ from the live transition authority')
            locked.guard()
            return source, result, start_sha256

    def _load_service_generation(self, run_id: str, checkpoint_sha256: str):
        """Read an exact generation already present in this run's transition chain."""
        token = _validate_sha256(checkpoint_sha256, "checkpoint generation SHA-256")
        with self._locked_run(run_id, create=False) as locked:
            current = self._load_from_paths(run_id, locked.run_sha256, locked.run_root, locked.current_path)
            if current.content_sha256 == token:
                return current
            matches = [item for item in current.transition_log if item.source_checkpoint_sha256 == token]
            if len(matches) != 1:
                raise ReferenceCheckpointConflictError('The preparation checkpoint is no longer available')
            result = self._load_generation_path(run_id,
                locked.run_root / f'{matches[0].revision - 1}-{token}.json')
            locked.guard()
            return result

    def create(self, checkpoint: ReferenceWorkflowCheckpoint) -> ReferenceWorkflowCheckpoint:
        return self._create(checkpoint, start_authority=None)[0]

    def _create_service_checkpoint(
        self,
        checkpoint: ReferenceWorkflowCheckpoint,
        target_input: ReferenceInputEvidence,
    ) -> tuple[ReferenceWorkflowCheckpoint, str]:
        run_sha256 = self._run_sha256(checkpoint.run_id)
        start_authority = _start_authority_bytes(run_sha256, target_input)
        return self._create(checkpoint, start_authority=start_authority)

    def _create(
        self,
        checkpoint: ReferenceWorkflowCheckpoint,
        *,
        start_authority: tuple[bytes, str] | None,
    ) -> tuple[ReferenceWorkflowCheckpoint, str | None]:
        if type(checkpoint) is not ReferenceWorkflowCheckpoint:
            raise TypeError("checkpoint must be a ReferenceWorkflowCheckpoint")
        if checkpoint.revision != 0 or checkpoint.prior_checkpoint_sha256 is not None:
            raise ReferenceCheckpointConflictError(
                "checkpoint create requires an initial revision without prior lineage"
            )
        with self._locked_run(checkpoint.run_id, create=True) as locked:
            run_sha256 = locked.run_sha256
            run_root = locked.run_root
            current_path = locked.current_path
            try:
                current = self._load_from_paths(
                    checkpoint.run_id, run_sha256, run_root, current_path
                )
            except ReferenceCheckpointStoreError:
                if current_path.exists() or current_path.is_symlink():
                    raise
            else:
                if current == checkpoint:
                    if start_authority is None:
                        return current, None
                    start_sha256 = self._load_start_authority(run_sha256, run_root, current)
                    if start_sha256 != start_authority[1]:
                        raise ReferenceCheckpointConflictError(
                            "reference start authority differs for existing run"
                        )
                    return current, start_sha256
                raise ReferenceCheckpointConflictError(
                    "checkpoint run already has different current authority"
                )
            generation_path = run_root / _generation_name(checkpoint)
            inventory, orphan_starts = _strict_orphan_inventory(run_root)
            if inventory and inventory != (generation_path,):
                raise ReferenceCheckpointStoreError(
                    "missing current pointer has foreign or multiple generations"
                )
            if start_authority is None:
                if orphan_starts:
                    raise ReferenceCheckpointStoreError(
                        "generic create cannot claim a service-bound orphan run"
                    )
                start_sha256 = None
            else:
                start_raw, start_sha256 = start_authority
                start_path = run_root / f"start-authority-{start_sha256}.json"
                start_inventory = orphan_starts
                if start_inventory and start_inventory != (start_path,):
                    raise ReferenceCheckpointConflictError(
                        "reference start authority differs for orphan run"
                    )
                _install_immutable(
                    start_path,
                    start_raw,
                    locked.guard,
                    directory=locked.directory_handle,
                    read_limit=_MAX_START_AUTHORITY_BYTES,
                    label="reference start authority",
                )
            raw = dumps_reference_workflow_checkpoint(checkpoint).encode("utf-8")
            _install_immutable(
                generation_path,
                raw,
                locked.guard,
                directory=locked.directory_handle,
                read_limit=_MAX_GENERATION_BYTES,
                label="checkpoint generation",
            )
            _replace_current_pointer(
                current_path,
                _pointer_bytes(checkpoint, run_sha256),
                locked.publication_lock,
                locked.guard,
                locked.directory_handle,
            )
            persisted = self._load_from_paths(checkpoint.run_id, run_sha256, run_root, current_path)
            if start_authority is not None:
                reopened = self._load_start_authority(run_sha256, run_root, persisted)
                if reopened != start_sha256:
                    raise ReferenceCheckpointStoreError(
                        "reference start authority changed after installation"
                    )
            return persisted, start_sha256

    def compare_and_swap(
        self,
        expected_content_sha256: str,
        checkpoint: ReferenceWorkflowCheckpoint,
    ) -> ReferenceWorkflowCheckpoint:
        return self._compare_and_swap(
            expected_content_sha256,
            checkpoint,
            expected_start_authority_sha256=None,
        )

    def _compare_and_swap_service(
        self,
        expected_content_sha256: str,
        checkpoint: ReferenceWorkflowCheckpoint,
        expected_start_authority_sha256: str,
    ) -> tuple[ReferenceWorkflowCheckpoint, str]:
        expected_start = _validate_sha256(
            expected_start_authority_sha256,
            "expected reference start authority SHA-256",
        )
        persisted = self._compare_and_swap(
            expected_content_sha256,
            checkpoint,
            expected_start_authority_sha256=expected_start,
        )
        return persisted, expected_start

    def _compare_and_swap(
        self,
        expected_content_sha256: str,
        checkpoint: ReferenceWorkflowCheckpoint,
        *,
        expected_start_authority_sha256: str | None,
    ) -> ReferenceWorkflowCheckpoint:
        expected = _validate_sha256(expected_content_sha256, "expected checkpoint SHA-256")
        if type(checkpoint) is not ReferenceWorkflowCheckpoint:
            raise TypeError("checkpoint must be a ReferenceWorkflowCheckpoint")
        with self._locked_run(checkpoint.run_id, create=False) as locked:
            run_sha256 = locked.run_sha256
            run_root = locked.run_root
            current_path = locked.current_path
            current = self._load_from_paths(checkpoint.run_id, run_sha256, run_root, current_path)
            if expected_start_authority_sha256 is not None:
                observed_start = self._load_start_authority(run_sha256, run_root, current)
                if observed_start != expected_start_authority_sha256:
                    raise ReferenceCheckpointConflictError(
                        "reference start authority changed before CAS"
                    )
            if current == checkpoint:
                return current
            if current.content_sha256 != expected:
                raise ReferenceCheckpointConflictError(
                    "checkpoint current authority differs from compare-and-swap expectation"
                )
            if checkpoint.prior_checkpoint_sha256 != current.content_sha256:
                raise ReferenceCheckpointConflictError(
                    "candidate prior checkpoint does not bind current authority"
                )
            if checkpoint.revision != current.revision + 1:
                raise ReferenceCheckpointConflictError(
                    "candidate revision is not the direct current successor"
                )
            raw = dumps_reference_workflow_checkpoint(checkpoint).encode("utf-8")
            _install_immutable(
                run_root / _generation_name(checkpoint),
                raw,
                locked.guard,
                directory=locked.directory_handle,
                read_limit=_MAX_GENERATION_BYTES,
                label="checkpoint generation",
            )
            _replace_current_pointer(
                current_path,
                _pointer_bytes(checkpoint, run_sha256),
                locked.publication_lock,
                locked.guard,
                locked.directory_handle,
            )
            return self._load_from_paths(checkpoint.run_id, run_sha256, run_root, current_path)


__all__ = [
    "ReferenceCheckpointConflictError",
    "ReferenceCheckpointStore",
    "ReferenceCheckpointStoreError",
]
