"""Durable owner boundary and runtime composition for batch training plans.

The `HeldOutEvaluationTargetAuthority` record carries `training_plan_id`,
`evaluation_split`, `freeze_review_sha256`, `classifier_id`, `classifier_kind` and its
remaining declared fields.

Training records keep subject roles, reference evidence, model settings and application
scope linked. Their validators and native adapters are called by the shared training service
before a fit, evaluation or accepted publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import secrets
from threading import RLock
from types import MappingProxyType
from typing import Literal
from weakref import ReferenceType, ref

from ..batch_analysis_preflight import MIF_PIPELINE_BATCH_RUNTIME_VERSION
from ..batch_artifacts import (
    AnalysisDomainBinding,
    ArtifactConfigurationBinding,
    ArtifactIdentityContract,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    DispatchMode,
    StageAdapterSpec,
    StageAdapterRegistry,
    artifact_ref_set_sha256,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
)
from ...pipeline_core.durable_records import (
    DurableDirectoryAuthority,
    DurableRecordError,
)

from ._shared import (
    BatchTrainingError,
    ClassifierKind,
    _binding_identity,
    _binding_sort_key,
    _canonical_bytes,
    _digest,
    _export_json,
    _integer,
    _sha256,
)
from .planning import (
    TrainingPlanSet,
    ValidatedTrainingPlanSetProjection,
    _issue_validated_training_plan_set_projection,
    _training_plan_set_compact_facts,
    training_plan_set_identity_contract,
)

MAX_TRAINING_PLAN_SET_BYTES = 16 * 1024 * 1024
_STORE_OBJECT_DIRECTORY = "objects"
_STORE_IDENTITY_FILE = ".mif-training-plan-set-store.json"
_STORE_IDENTITY_SCHEMA = "mif-training-plan-set-store/v1"
_LOCATOR_NAME = re.compile(r"[0-9a-f]{64}\.json\Z")
_PRODUCER_VERSION = "mif-pipeline-batch-training-plan-set/v1"
_CREATE_STORE_DIRECTORIES = os.name == "posix"
_NODE_KIND_BY_CLASSIFIER = {
    ClassifierKind.PRIMARY_COMPARTMENT: ("primary_classifier_training_plan_review"),
    ClassifierKind.SECONDARY_STRUCTURE: ("secondary_classifier_training_plan_review"),
    ClassifierKind.SUPERVISED_PHENOTYPE: ("supervised_phenotype_training_plan_review"),
}


# ==============================================================================
#                                    Runtime
# ==============================================================================


class _HeldOutEvaluationTargetIssuer:
    """Own one-use in-memory evaluation target capabilities."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._live: dict[str, tuple[ReferenceType[object], str]] = {}

    def issue(self, authority: object, initialize: object) -> None:
        if not callable(initialize):
            raise BatchTrainingError("held-out target issuer initialization is invalid")
        with self._lock:
            nonce = secrets.token_hex(32)
            while nonce in self._live:
                nonce = secrets.token_hex(32)
            initialize(nonce, id(authority))
            seal = getattr(authority, "_seal_sha256", None)
            if type(seal) is not str:
                raise BatchTrainingError("held-out target issuance seal is invalid")

            def remove_expired(
                expired: ReferenceType[object],
                *,
                target_nonce: str = nonce,
                issuance_seal: str = seal,
            ) -> None:
                with self._lock:
                    current = self._live.get(target_nonce)
                    if (
                        current is not None
                        and current[0] is expired
                        and current[1] == issuance_seal
                    ):
                        self._live.pop(target_nonce, None)

            self._live[nonce] = (ref(authority, remove_expired), seal)

    def consume(self, authority: object, reopen: object) -> object:
        if not callable(reopen):
            raise BatchTrainingError("held-out target reauthentication is invalid")
        with self._lock:
            registered_nonce: str | None = None
            try:
                for nonce, candidate in self._live.items():
                    if candidate[0]() is authority:
                        registered_nonce = nonce
                        break
                if registered_nonce is None:
                    raise BatchTrainingError("held-out evaluation target is foreign or consumed")
                live = self._live[registered_nonce]
                if (
                    live[0]() is not authority
                    or getattr(authority, "_object_identity", None) != id(authority)
                    or getattr(authority, "_seal_sha256", None) != live[1]
                ):
                    raise BatchTrainingError("held-out evaluation target is foreign or consumed")
                return reopen()
            except Exception as exc:
                raise BatchTrainingError(
                    "held-out evaluation target was consumed after failed reauthentication; "
                    "open a fresh target"
                ) from exc
            finally:
                if registered_nonce is not None:
                    live = self._live.get(registered_nonce)
                    if live is not None and live[0]() is authority:
                        self._live.pop(registered_nonce, None)


_HELD_OUT_EVALUATION_TARGET_ISSUER = _HeldOutEvaluationTargetIssuer()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False, repr=False)
class HeldOutEvaluationTargetAuthority:
    """Opaque one-use binding of current held-out evidence to one native fit."""

    training_plan_id: str
    evaluation_split: Literal["validation", "test"]
    freeze_review_sha256: str | None
    classifier_id: str
    classifier_kind: ClassifierKind
    validation_domain_id: str
    required_classes: tuple[str, ...]
    fit_plan_sha256: str
    fit_execution_sha256: str
    checkpoint_entry_sha256: str
    checkpoint_set_sha256: str
    state_head_sha256: str
    active_plan_sha256: str
    _parent: object
    _context: object
    _state_store: object
    _engine_registry: object
    _held_out_authority: object
    _held_out_evidence: object
    _checkpoint_entry: object
    _runtime_authority: object
    _source_seal_sha256: str
    _snapshot: Mapping[str, object]
    _nonce: str
    _object_identity: int
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("HeldOutEvaluationTargetAuthority is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("HeldOutEvaluationTargetAuthority cannot be subclassed")

    def __copy__(self) -> object:
        raise TypeError("HeldOutEvaluationTargetAuthority is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("HeldOutEvaluationTargetAuthority is noncopyable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("HeldOutEvaluationTargetAuthority is nonserializable")


def _held_out_target_seal(snapshot: Mapping[str, object], nonce: str, identity: int) -> str:
    return _digest({"snapshot": snapshot, "nonce": nonce, "object_identity": identity})


def _require_held_out_evidence(
    authority: object,
    evidence: object,
    *,
    classifier_kind: ClassifierKind,
) -> None:
    """Reject any held-out material that is empty, mismatched, or training-role."""

    from .evidence import (
        PixelHeldOutEvaluationAuthority,
        SupervisedHeldOutEvaluationAuthority,
    )

    is_supervised = classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE
    if (is_supervised and type(authority) is not SupervisedHeldOutEvaluationAuthority) or (
        not is_supervised and type(authority) is not PixelHeldOutEvaluationAuthority
    ):
        raise BatchTrainingError("held-out authority family differs from training plan")
    entries = (
        tuple(getattr(evidence, "entries", ()))
        if is_supervised
        else tuple(getattr(evidence, "entry_states", ()))
    )
    if not entries or any(
        getattr(item, "split", None) not in {"validation", "test"} for item in entries
    ):
        raise BatchTrainingError("held-out evidence must contain only validation or test coverage")
    if not is_supervised:
        rows = tuple(getattr(evidence, "rows", ()))
        if not rows or any(
            getattr(item, "split", None) not in {"validation", "test"} for item in rows
        ):
            raise BatchTrainingError("pixel held-out evidence rows are invalid")


def _select_held_out_target_fit(
    *,
    parent: object,
    current_fit_set: object,
    plan: object,
    expected_entry: object | None = None,
    expected_runtime: object | None = None,
) -> tuple[object, object]:
    """Select the sole checkpoint/runtime pair by the plan's complete identity."""

    from ..batch_state import CurrentNativeFitSetAuthority
    from .engines import TrainingFitPlanProjection
    from ..batch_training_checkpoint import (
        BatchNativeFitCheckpointEntry,
        BatchNativeFitCheckpointSet,
    )
    from mif_pipeline.qupath_runtime.native_classifier_fit import QuPathNativeClassifierFitAuthority

    if type(current_fit_set) is not CurrentNativeFitSetAuthority:
        raise BatchTrainingError("current native fit set authority is invalid")
    checkpoint_set = current_fit_set.checkpoint_set
    runtime_authorities = tuple(current_fit_set.runtime_authorities)
    if (
        type(checkpoint_set) is not BatchNativeFitCheckpointSet
        or len(checkpoint_set.entries) != len(runtime_authorities)
        or not runtime_authorities
    ):
        raise BatchTrainingError("current native fit checkpoint/runtime correspondence is invalid")
    projection = TrainingFitPlanProjection.create(plan)
    matches: list[tuple[object, object]] = []
    for entry, runtime in zip(checkpoint_set.entries, runtime_authorities, strict=True):
        if (
            type(entry) is not BatchNativeFitCheckpointEntry
            or type(runtime) is not QuPathNativeClassifierFitAuthority
        ):
            raise BatchTrainingError("current native fit checkpoint material is invalid")
        if (
            entry.classifier_id == projection.classifier_id
            and entry.classifier_kind == projection.classifier_kind.value
            and entry.fit_plan_sha256 == projection.fit_plan_sha256
        ):
            matches.append((entry, runtime))
    if len(matches) != 1:
        raise BatchTrainingError("current native fit set has no unique selected classifier fit")
    entry, runtime = matches[0]
    material = runtime.material_record
    expected_native_kind = (
        "supervised_phenotype"
        if projection.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE
        else "pixel"
    )
    expected_native_type = (
        "object" if projection.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE else "pixel"
    )
    if (
        current_fit_set.state_head_sha256 != parent.state_head_sha256
        or current_fit_set.active_plan_sha256 != parent.active_plan_sha256
        or current_fit_set._current_execution != parent._current_execution
        or checkpoint_set.validation_domain_id != parent.validation_domain_id
        or checkpoint_set.training_plan_set_sha256
        != parent.training_plan_set.plan_set.training_plan_set_sha256
        or entry.classifier_id != projection.classifier_id
        or entry.classifier_kind != projection.classifier_kind.value
        or entry.fit_plan_sha256 != projection.fit_plan_sha256
        or entry.fit_execution_sha256 != material.fit_execution_sha256
        or entry.training_material_evidence_sha256 != material.training_material_evidence_sha256
        or entry.native_classifier_type != expected_native_type
        or material.classifier_id != projection.classifier_id
        or material.classifier_kind != expected_native_kind
        or material.fit_plan_sha256 != projection.fit_plan_sha256
        or material.classifier_version != entry.classifier_version
        or material.native_classifier_sha256 != entry.native_classifier_sha256
        or material.native_classifier_byte_count != entry.native_classifier_byte_count
        or material.project_relative_path != entry.project_relative_path
        or material.project_identity_sha256 != entry.project_identity_sha256
        or material.fit_material_sha256 != entry.runtime_fit_material_sha256
        or runtime.mutation_attestation_sha256 != entry.mutation_attestation_sha256
        or runtime.successor_project_authority_head_sha256
        != entry.successor_project_authority_head_sha256
        or (expected_entry is not None and entry != expected_entry)
        or (
            expected_runtime is not None
            and runtime.material_record != expected_runtime.material_record
        )
        or (
            expected_runtime is not None
            and runtime.mutation_attestation_sha256 != expected_runtime.mutation_attestation_sha256
        )
        or (
            expected_runtime is not None
            and runtime.successor_project_authority_head_sha256
            != expected_runtime.successor_project_authority_head_sha256
        )
    ):
        raise BatchTrainingError("selected native fit differs from current authenticated material")
    return entry, runtime


def open_held_out_evaluation_target(
    *,
    current_parent: object,
    context: BatchNodeExecutionContext,
    state_store: object,
    engine_registry: object,
    held_out_authority: object,
    evaluation_split: Literal["validation", "test"],
) -> HeldOutEvaluationTargetAuthority:
    """Issue a process-local one-use target from current owner-selected material."""

    from ..batch_state import BatchStateStore, CurrentNativeFitSetAuthority
    from .engines import TrainingEngineProviderRegistry
    from .evidence import PixelHeldOutEvaluationAuthority, SupervisedHeldOutEvaluationAuthority
    from .parent_execution import (
        CurrentTrainingParentExecution,
        validate_current_training_parent_execution,
    )
    from .planning import TrainingPlan
    from .record_publication import require_training_evaluation_phase

    if evaluation_split not in ("validation", "test"):
        raise BatchTrainingError("held-out evaluation split must be validation or test")

    if (
        type(current_parent) is not CurrentTrainingParentExecution
        or type(context) is not BatchNodeExecutionContext
        or type(state_store) is not BatchStateStore
        or type(engine_registry) is not TrainingEngineProviderRegistry
        or type(held_out_authority)
        not in {PixelHeldOutEvaluationAuthority, SupervisedHeldOutEvaluationAuthority}
    ):
        raise BatchTrainingError("held-out evaluation target requires exact owner authorities")
    try:
        parent = validate_current_training_parent_execution(
            current_parent, expected_context=context, state_store=state_store
        )
        evidence = held_out_authority.reopen(
            parent_execution=parent, expected_context=context, state_store=state_store
        )
        training_plan_id = held_out_authority._training_plan_id
        plan = parent.training_plan_set.require_plan(training_plan_id)
        if type(plan) is not TrainingPlan:
            raise BatchTrainingError("held-out target plan is invalid")
        if not plan.split_project_entry_ids[evaluation_split]:
            raise BatchTrainingError("selected evaluation split has no plan entries")
        _require_held_out_evidence(
            held_out_authority, evidence, classifier_kind=plan.request.classifier_kind
        )
        current_fit_set = state_store.reopen_current_native_fit_set(
            context, current_parent=parent, engine_registry=engine_registry
        )
        if type(current_fit_set) is not CurrentNativeFitSetAuthority:
            raise BatchTrainingError("held-out evaluation target requires a current native fit")
        entry, runtime = _select_held_out_target_fit(
            parent=parent, current_fit_set=current_fit_set, plan=plan
        )
        freeze_review_sha256 = require_training_evaluation_phase(
            state_store=state_store,
            context=context,
            current_parent=parent,
            current_fit_set=current_fit_set,
            plan=plan,
            evaluation_split=evaluation_split,
        )
    except Exception as exc:
        if isinstance(exc, BatchTrainingError):
            raise
        raise BatchTrainingError("held-out evaluation target authority is not authentic") from exc
    snapshot = MappingProxyType(
        {
            "parent_identity": id(parent),
            "context_identity": id(context),
            "state_store_identity": id(state_store),
            "engine_registry_identity": id(engine_registry),
            "held_out_authority_identity": id(held_out_authority),
            "held_out_source_seal_sha256": held_out_authority._seal_sha256,
            "parent_seal_sha256": parent._seal_sha256,
            "training_plan_id": plan.training_plan_id,
            "evaluation_split": evaluation_split,
            "freeze_review_sha256": freeze_review_sha256,
            "classifier_id": plan.request.classifier_id,
            "classifier_kind": plan.request.classifier_kind.value,
            "required_classes": list(plan.request.required_classes),
            "validation_domain_id": parent.validation_domain_id,
            "fit_plan_sha256": entry.fit_plan_sha256,
            "fit_execution_sha256": entry.fit_execution_sha256,
            "checkpoint_entry_sha256": entry.checkpoint_entry_sha256,
            "checkpoint_set_sha256": current_fit_set.checkpoint_set.checkpoint_set_sha256,
            "state_head_sha256": parent.state_head_sha256,
            "active_plan_sha256": parent.active_plan_sha256,
            "runtime_fit_material_sha256": entry.runtime_fit_material_sha256,
            "mutation_attestation_sha256": entry.mutation_attestation_sha256,
            "successor_project_authority_head_sha256": entry.successor_project_authority_head_sha256,
        }
    )
    target = object.__new__(HeldOutEvaluationTargetAuthority)
    values = {
        "training_plan_id": plan.training_plan_id,
        "classifier_id": plan.request.classifier_id,
        "evaluation_split": evaluation_split,
        "freeze_review_sha256": freeze_review_sha256,
        "classifier_kind": plan.request.classifier_kind,
        "validation_domain_id": parent.validation_domain_id,
        "required_classes": plan.request.required_classes,
        "fit_plan_sha256": entry.fit_plan_sha256,
        "fit_execution_sha256": entry.fit_execution_sha256,
        "checkpoint_entry_sha256": entry.checkpoint_entry_sha256,
        "checkpoint_set_sha256": current_fit_set.checkpoint_set.checkpoint_set_sha256,
        "state_head_sha256": parent.state_head_sha256,
        "active_plan_sha256": parent.active_plan_sha256,
        "_parent": parent,
        "_context": context,
        "_state_store": state_store,
        "_engine_registry": engine_registry,
        "_held_out_authority": held_out_authority,
        "_held_out_evidence": evidence,
        "_checkpoint_entry": entry,
        "_runtime_authority": runtime,
        "_source_seal_sha256": held_out_authority._seal_sha256,
        "_snapshot": snapshot,
        "_nonce": "",
        "_object_identity": 0,
        "_seal_sha256": "",
        "_issuer": _HELD_OUT_EVALUATION_TARGET_ISSUER,
    }
    for name, value in values.items():
        object.__setattr__(target, name, value)

    def initialize(nonce: str, identity: int) -> None:
        object.__setattr__(target, "_nonce", nonce)
        object.__setattr__(target, "_object_identity", identity)
        object.__setattr__(target, "_seal_sha256", _held_out_target_seal(snapshot, nonce, identity))

    _HELD_OUT_EVALUATION_TARGET_ISSUER.issue(target, initialize)
    return target


def _reopen_held_out_evaluation_target(
    authority: object,
    *,
    expected_plan: object | None = None,
    expected_evidence: object | None = None,
    expected_checkpoint_entry: object | None = None,
    expected_fit_authority: object | None = None,
) -> tuple[object, object, object, object]:
    """Reopen the exact current target material without issuing a capability."""

    if (
        type(authority) is not HeldOutEvaluationTargetAuthority
        or authority._issuer is not _HELD_OUT_EVALUATION_TARGET_ISSUER
        or authority._object_identity != id(authority)
        or authority._seal_sha256
        != _held_out_target_seal(authority._snapshot, authority._nonce, authority._object_identity)
    ):
        raise BatchTrainingError("held-out evaluation target seal is invalid")
    parent = authority._parent
    context = authority._context
    state_store = authority._state_store
    registry = authority._engine_registry
    if (
        id(context) != authority._snapshot["context_identity"]
        or id(state_store) != authority._snapshot["state_store_identity"]
        or id(registry) != authority._snapshot["engine_registry_identity"]
        or id(authority._held_out_authority) != authority._snapshot["held_out_authority_identity"]
        or authority.training_plan_id != authority._snapshot["training_plan_id"]
        or authority.evaluation_split != authority._snapshot["evaluation_split"]
        or authority.freeze_review_sha256 != authority._snapshot["freeze_review_sha256"]
        or authority.classifier_id != authority._snapshot["classifier_id"]
        or authority.classifier_kind.value != authority._snapshot["classifier_kind"]
        or tuple(authority.required_classes) != tuple(authority._snapshot["required_classes"])
        or authority.validation_domain_id != authority._snapshot["validation_domain_id"]
        or authority.fit_plan_sha256 != authority._snapshot["fit_plan_sha256"]
        or authority.fit_execution_sha256 != authority._snapshot["fit_execution_sha256"]
        or authority.checkpoint_entry_sha256 != authority._snapshot["checkpoint_entry_sha256"]
        or authority.checkpoint_set_sha256 != authority._snapshot["checkpoint_set_sha256"]
        or authority.state_head_sha256 != authority._snapshot["state_head_sha256"]
        or authority.active_plan_sha256 != authority._snapshot["active_plan_sha256"]
    ):
        raise BatchTrainingError("held-out evaluation target snapshot was mutated")
    from .parent_execution import validate_current_training_parent_execution

    parent = validate_current_training_parent_execution(
        parent, expected_context=context, state_store=state_store
    )
    if (
        id(parent) != authority._snapshot["parent_identity"]
        or parent._seal_sha256 != authority._snapshot["parent_seal_sha256"]
        or authority._source_seal_sha256 != authority._snapshot["held_out_source_seal_sha256"]
    ):
        raise BatchTrainingError("held-out evaluation target parent identity changed")
    evidence = authority._held_out_authority.reopen(
        parent_execution=parent, expected_context=context, state_store=state_store
    )
    if (
        evidence != authority._held_out_evidence
        or authority._held_out_authority._seal_sha256 != authority._source_seal_sha256
    ):
        raise BatchTrainingError("held-out evaluation target evidence is stale")
    plan = parent.training_plan_set.require_plan(authority.training_plan_id)
    _require_held_out_evidence(
        authority._held_out_authority,
        evidence,
        classifier_kind=plan.request.classifier_kind,
    )
    current_fit_set = state_store.reopen_current_native_fit_set(
        context, current_parent=parent, engine_registry=registry
    )
    if current_fit_set is None:
        raise BatchTrainingError("held-out evaluation target has no current native fit")
    entry, runtime = _select_held_out_target_fit(
        parent=parent,
        current_fit_set=current_fit_set,
        plan=plan,
        expected_entry=authority._checkpoint_entry,
        expected_runtime=authority._runtime_authority,
    )
    from .record_publication import require_training_evaluation_phase

    freeze_review_sha256 = require_training_evaluation_phase(
        state_store=state_store,
        context=context,
        current_parent=parent,
        current_fit_set=current_fit_set,
        plan=plan,
        evaluation_split=authority.evaluation_split,
    )
    if freeze_review_sha256 != authority.freeze_review_sha256:
        raise BatchTrainingError("held-out evaluation freeze authority changed")
    if (
        current_fit_set.checkpoint_set.checkpoint_set_sha256 != authority.checkpoint_set_sha256
        or parent.state_head_sha256 != authority.state_head_sha256
        or parent.active_plan_sha256 != authority.active_plan_sha256
    ):
        raise BatchTrainingError("held-out evaluation target state is stale")
    if (
        (expected_plan is not None and plan != expected_plan)
        or (expected_evidence is not None and evidence != expected_evidence)
        or (expected_checkpoint_entry is not None and entry != expected_checkpoint_entry)
        or (
            expected_fit_authority is not None
            and (
                runtime.material_record != getattr(expected_fit_authority, "material_record", None)
                or runtime.mutation_attestation_sha256
                != getattr(expected_fit_authority, "mutation_attestation_sha256", None)
                or runtime.successor_project_authority_head_sha256
                != getattr(
                    expected_fit_authority,
                    "successor_project_authority_head_sha256",
                    None,
                )
            )
        )
    ):
        raise BatchTrainingError("held-out evaluation target changed during read-only evaluation")
    return plan, evidence, entry, runtime


def _consume_held_out_evaluation_target(
    authority: object,
) -> tuple[object, object, object, object]:
    """Reauthenticate and consume a target for the later R2.3 provider owner."""

    result = _HELD_OUT_EVALUATION_TARGET_ISSUER.consume(
        authority, lambda: _reopen_held_out_evaluation_target(authority)
    )
    if type(result) is not tuple or len(result) != 4:
        raise BatchTrainingError("held-out evaluation target consumption returned invalid material")
    return result


def _revalidate_consumed_held_out_evaluation_target(
    authority: object,
    expected_plan: object,
    expected_evidence: object,
    expected_checkpoint_entry: object,
    expected_fit_authority: object,
) -> None:
    """Prove the consumed target still selects the same current owner material."""

    _reopen_held_out_evaluation_target(
        authority,
        expected_plan=expected_plan,
        expected_evidence=expected_evidence,
        expected_checkpoint_entry=expected_checkpoint_entry,
        expected_fit_authority=expected_fit_authority,
    )


def _canonical_store_root(root: Path) -> Path:
    if not isinstance(root, Path):
        raise BatchTrainingError("training plan set store root must be a Path")
    if not root.is_absolute():
        raise BatchTrainingError("training plan set store root must be an absolute path")
    if root == Path(root.anchor):
        raise BatchTrainingError("training plan set store root cannot be a filesystem root")
    if ".." in root.parts:
        raise BatchTrainingError("training plan set store root must be resolved canonical path")
    return root


def _canonical_store_locator(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchTrainingError("training plan set store locator must be canonical relative text")
    if "\\" in value or any(ord(character) < 32 for character in value):
        raise BatchTrainingError("training plan set store locator must use safe POSIX text")
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    segments = value.split("/")
    if (
        windows.drive
        or windows.root
        or posix.is_absolute()
        or value.startswith("//")
        or any(segment in {"", ".", ".."} for segment in segments)
        or posix.as_posix() != value
        or len(posix.parts) != 2
        or posix.parts[0] != _STORE_OBJECT_DIRECTORY
        or _LOCATOR_NAME.fullmatch(posix.parts[1]) is None
    ):
        raise BatchTrainingError(
            "training plan set store locator must be a safe canonical store-relative path"
        )
    return value


def provision_training_plan_set_store_root(root: Path) -> Path:
    """Provision the retained store directories before opening the store owner."""

    canonical = _canonical_store_root(root)
    if canonical != canonical.resolve(strict=False):
        raise BatchTrainingError("training plan set store root must be resolved canonical path")
    if not _CREATE_STORE_DIRECTORIES:
        from ...pipeline_core.win32_authority_provisioning import (
            provision_windows_current_user_directory_closure,
        )
        from ...pipeline_core.win32_file_authority import Win32FileAuthorityError

        try:
            provisioned = provision_windows_current_user_directory_closure(
                canonical,
                child_names=(_STORE_OBJECT_DIRECTORY,),
            )
        except (DurableRecordError, Win32FileAuthorityError, OSError) as exc:
            raise BatchTrainingError(
                "training plan set store root cannot be provisioned safely"
            ) from exc
        if provisioned != canonical:
            raise BatchTrainingError(
                "training plan set store provisioning changed its canonical root"
            )
        return canonical
    root_authority: DurableDirectoryAuthority | None = None
    objects_authority: DurableDirectoryAuthority | None = None
    try:
        root_authority = DurableDirectoryAuthority(canonical, create=True)
        objects_authority = root_authority.retain_child(Path(_STORE_OBJECT_DIRECTORY), create=True)
    except (DurableRecordError, OSError) as exc:
        raise BatchTrainingError(
            "training plan set store root cannot be provisioned safely"
        ) from exc
    finally:
        if objects_authority is not None:
            objects_authority.close()
        if root_authority is not None:
            root_authority.close()
    return canonical


class TrainingPlanSetStore:
    """Retained durable owner for immutable canonical training-plan-set bodies.

    The shared authority supplies no-follow retained directory capabilities,
    bounded immutable reads, exclusive publication, and flushed file bytes.
    POSIX can create this caller-designated store and synchronizes its directory
    entries.  Windows opens an installer-provisioned root and ``objects`` child;
    it does not claim standalone directory-metadata power-loss durability.
    """

    def __init__(self, root: Path, *, _imported_identity_bytes: bytes | None = None) -> None:
        """Open an existing store or initialize one for a new analysis.

        Import composition supplies the exact marker from its validated capsule.
        We retain that identity while its ordinary import lease publishes the
        missing marker and bodies; construction must not invent another marker.
        An already published marker must match those original bytes exactly.
        """
        if _imported_identity_bytes is not None:
            if type(_imported_identity_bytes) is not bytes:
                raise BatchTrainingError("imported setup identity must be bytes")
            self._parse_store_identity(_imported_identity_bytes)
        self._root = _canonical_store_root(root)
        self._root_authority: DurableDirectoryAuthority | None = None
        self._objects_authority: DurableDirectoryAuthority | None = None
        try:
            root_authority = DurableDirectoryAuthority(self._root, create=_CREATE_STORE_DIRECTORIES)
            self._root_authority = root_authority
            self._objects_authority = root_authority.retain_child(
                Path(_STORE_OBJECT_DIRECTORY),
                create=_CREATE_STORE_DIRECTORIES,
            )
            marker = root_authority.read_immutable(
                _STORE_IDENTITY_FILE,
                max_bytes=1024,
                label="training plan set store identity",
                optional=True,
            )
            marker_was_present = marker is not None
            if _imported_identity_bytes is not None:
                if marker is not None and marker != _imported_identity_bytes:
                    raise BatchTrainingError("imported setup store differs from its existing identity")
                marker = _imported_identity_bytes
            elif marker is None:
                marker = _canonical_bytes(
                    {
                        "schema_version": _STORE_IDENTITY_SCHEMA,
                        "store_nonce_sha256": secrets.token_hex(32),
                    }
                )
                root_authority.publish_immutable(
                    _STORE_IDENTITY_FILE,
                    marker,
                    max_bytes=1024,
                    label="training plan set store identity",
                )
            self._store_identity_sha256 = self._parse_store_identity(marker)
            if marker_was_present or _imported_identity_bytes is None:
                self._validate_store_identity()
        except (DurableRecordError, OSError) as exc:
            self._close_after_initialization_failure()
            raise BatchTrainingError(
                "training plan set store directory cannot be retained safely "
                "(including symlink traversal)"
            ) from exc
        except BaseException:
            self._close_after_initialization_failure()
            raise

    @property
    def root(self) -> Path:
        return self._root

    @property
    def store_identity_sha256(self) -> str:
        return self._store_identity_sha256

    @staticmethod
    def _parse_store_identity(content: bytes) -> str:
        try:
            mapping = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchTrainingError("training plan set store identity is unreadable") from exc
        if (
            not isinstance(mapping, dict)
            or set(mapping) != {"schema_version", "store_nonce_sha256"}
            or mapping.get("schema_version") != _STORE_IDENTITY_SCHEMA
        ):
            raise BatchTrainingError("training plan set store identity schema is invalid")
        _sha256(
            mapping.get("store_nonce_sha256"),
            "training plan set store nonce",
        )
        if _canonical_bytes(mapping) != content:
            raise BatchTrainingError("training plan set store identity is not canonical")
        return hashlib.sha256(content).hexdigest()

    def _require_authorities(
        self,
    ) -> tuple[DurableDirectoryAuthority, DurableDirectoryAuthority]:
        root = self._root_authority
        objects = self._objects_authority
        if root is None or objects is None:
            raise BatchTrainingError("training plan set store is closed")
        return root, objects

    def _validate_store_identity(self) -> None:
        root, _ = self._require_authorities()
        try:
            content = root.read_immutable(
                _STORE_IDENTITY_FILE,
                max_bytes=1024,
                label="training plan set store identity",
            )
        except (DurableRecordError, OSError) as exc:
            raise BatchTrainingError(
                "training plan set store identity cannot be reopened safely"
            ) from exc
        if content is None or self._parse_store_identity(content) != (self._store_identity_sha256):
            raise BatchTrainingError("training plan set store owner identity changed")

    def _body_leaf(self, locator: object, *, expected_sha256: str) -> tuple[str, str]:
        self._validate_store_identity()
        relative = _canonical_store_locator(locator)
        digest = _sha256(expected_sha256, "training plan set body_sha256")
        leaf = PurePosixPath(relative).name
        if leaf != f"{digest}.json":
            raise BatchTrainingError("training plan set store locator differs from body SHA-256")
        return relative, leaf

    def publish(self, plan_set: TrainingPlanSet) -> tuple[str, str, int]:
        if type(plan_set) is not TrainingPlanSet:
            raise BatchTrainingError("training plan set store requires an exact TrainingPlanSet")
        body = _canonical_bytes(plan_set.to_mapping())
        byte_count = len(body)
        if byte_count <= 0 or byte_count > MAX_TRAINING_PLAN_SET_BYTES:
            raise BatchTrainingError("training plan set body exceeds the durable store size bound")
        body_sha256 = hashlib.sha256(body).hexdigest()
        locator = f"{_STORE_OBJECT_DIRECTORY}/{body_sha256}.json"
        _, leaf = self._body_leaf(locator, expected_sha256=body_sha256)
        _, objects = self._require_authorities()
        try:
            objects.publish_immutable(
                leaf,
                body,
                max_bytes=MAX_TRAINING_PLAN_SET_BYTES,
                label="training plan set body",
            )
        except (DurableRecordError, OSError) as exc:
            raise BatchTrainingError(
                "training plan set body cannot be published immutably"
            ) from exc
        return locator, body_sha256, byte_count

    def reopen(
        self,
        locator: str,
        *,
        sha256: str,
        byte_count: int,
    ) -> TrainingPlanSet:
        expected_sha256 = _sha256(sha256, "training plan set body_sha256")
        expected_count = _integer(byte_count, "training plan set body_byte_count")
        if expected_count <= 0 or expected_count > MAX_TRAINING_PLAN_SET_BYTES:
            raise BatchTrainingError("training plan set body exceeds the durable store size bound")
        _, leaf = self._body_leaf(locator, expected_sha256=expected_sha256)
        _, objects = self._require_authorities()
        try:
            body = objects.read_immutable(
                leaf,
                max_bytes=MAX_TRAINING_PLAN_SET_BYTES,
                label="training plan set body",
            )
        except (DurableRecordError, OSError) as exc:
            raise BatchTrainingError(
                "training plan set body is missing or not one canonical bounded "
                "regular file; a symlink, content change, SHA-256 mismatch, or "
                "size bound is unsafe"
            ) from exc
        assert body is not None
        if len(body) != expected_count:
            raise BatchTrainingError("training plan set body does not match its byte count")
        if hashlib.sha256(body).hexdigest() != expected_sha256:
            raise BatchTrainingError("training plan set body does not match its SHA-256")
        try:
            decoded = json.loads(
                body.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid constant {value}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise BatchTrainingError("training plan set body is not finite UTF-8 JSON") from exc
        return TrainingPlanSet.from_mapping(decoded)

    def _close_after_initialization_failure(self) -> None:
        try:
            self.close()
        except BaseException:
            pass

    def close(self) -> None:
        objects = self._objects_authority
        root = self._root_authority
        self._objects_authority = None
        self._root_authority = None
        failure: BaseException | None = None
        for authority in (objects, root):
            if authority is None:
                continue
            try:
                authority.close()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if failure is None:
            return
        if isinstance(failure, (DurableRecordError, OSError)):
            raise BatchTrainingError(
                "training plan set store capabilities cannot be closed"
            ) from failure
        raise failure

    def __enter__(self) -> "TrainingPlanSetStore":
        self._require_authorities()
        return self

    def __exit__(self, *_exc: object) -> None:
        active = _exc[1] if len(_exc) > 1 else None
        try:
            self.close()
        except BaseException as cleanup:
            if isinstance(active, BaseException):
                add_note = getattr(active, "add_note", None)
                if callable(add_note):
                    add_note("training plan set store cleanup failure: " f"{cleanup!r}")
                return
            raise

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, OSError, BatchTrainingError):
            pass


def _canonical_bindings(
    values: tuple[AnalysisDomainBinding, ...],
) -> tuple[AnalysisDomainBinding, ...]:
    by_identity: dict[bytes, AnalysisDomainBinding] = {}
    for binding in values:
        identity = _binding_identity(binding)
        by_identity.setdefault(identity, binding)
    return tuple(sorted(by_identity.values(), key=_binding_sort_key))


def _context_configuration_bindings(
    input_refs: tuple[BatchArtifactRef, ...],
    participant_ids: tuple[str, ...],
) -> tuple[ArtifactConfigurationBinding, ...]:
    by_sample: dict[str, ArtifactConfigurationBinding] = {}
    participants = set(participant_ids)
    for ref in input_refs:
        if ref.scope.analysis_domain_bindings and any(
            item.sample_id not in participants for item in ref.configuration_bindings
        ):
            raise BatchTrainingError(
                "training plan set domain input has a foreign participant configuration"
            )
        for binding in ref.configuration_bindings:
            if binding.sample_id not in participants:
                continue
            previous = by_sample.setdefault(binding.sample_id, binding)
            if previous != binding:
                raise BatchTrainingError(
                    "training plan set inputs disagree on participant configuration"
                )
    if set(by_sample) != participants:
        raise BatchTrainingError(
            "training plan set inputs lack exact participant configuration coverage"
        )
    return tuple(by_sample[item] for item in sorted(by_sample))


def build_training_plan_set_execution_result(
    context: BatchNodeExecutionContext,
    *,
    plan_set: TrainingPlanSet,
    store: TrainingPlanSetStore,
) -> BatchNodeExecutionResult:
    """Publish one scheduler-authorized plan set through its explicit owner."""

    if type(context) is not BatchNodeExecutionContext:
        raise BatchTrainingError(
            "training plan set publication requires an exact execution context"
        )
    if type(plan_set) is not TrainingPlanSet:
        raise BatchTrainingError("plan_set must be an exact TrainingPlanSet")
    if type(store) is not TrainingPlanSetStore:
        raise BatchTrainingError("training plan set publication requires its exact owner store")
    facts = _training_plan_set_compact_facts(plan_set)
    expected_node_kind = _NODE_KIND_BY_CLASSIFIER[plan_set.classifier_kind]
    if context.adapter_spec.node_kind != expected_node_kind:
        if context.adapter_spec.node_kind not in _NODE_KIND_BY_CLASSIFIER.values():
            raise BatchTrainingError("training plan set context has a foreign node kind")
        raise BatchTrainingError("training plan set node classifier kind differs from its plan set")
    contract = training_plan_set_identity_contract()
    if (
        context.adapter_spec.dispatch_mode is not DispatchMode.SINGLE
        or context.adapter_spec.declared_outputs != ("training_plan_set",)
        or context.adapter_spec.output_identity_contracts != (contract,)
    ):
        raise BatchTrainingError("training plan set context lacks its exact owner output contract")
    authorized_input_refs = tuple(context.inputs)
    if not authorized_input_refs:
        raise BatchTrainingError("training plan set publication requires source inputs")
    source_ref_sha256s = facts["source_artifact_ref_sha256s"]
    authorized_by_sha = {item.artifact_ref_sha256: item for item in authorized_input_refs}
    try:
        input_refs = tuple(authorized_by_sha[item] for item in source_ref_sha256s)
    except KeyError as exc:
        raise BatchTrainingError(
            "training plan set selected source is outside the authorized input set"
        ) from exc
    if (
        not isinstance(source_ref_sha256s, tuple)
        or tuple(sorted(item.artifact_ref_sha256 for item in input_refs)) != source_ref_sha256s
        or artifact_ref_set_sha256(input_refs) != facts["source_artifact_ref_set_sha256"]
    ):
        raise BatchTrainingError("training plan set sources differ from the authorized input set")
    if any(item.scope.validation_domain_id != plan_set.validation_domain_id for item in input_refs):
        raise BatchTrainingError(
            "training plan set input validation domain differs from its plan set"
        )
    participant_ids = facts["participant_ids"]
    project_entry_ids = facts["project_entry_ids"]
    if not set(participant_ids) <= set(context.authorization.dispatch_binding.participant_ids):
        raise BatchTrainingError("training plan set participants differ from dispatch authority")
    if not set(project_entry_ids) <= set(context.authorization.dispatch_binding.project_entry_ids):
        raise BatchTrainingError("training plan set project entries differ from dispatch authority")
    expected_bindings = facts["analysis_domain_bindings"]
    input_bindings = _canonical_bindings(
        tuple(binding for item in input_refs for binding in item.scope.analysis_domain_bindings)
    )
    if input_bindings != expected_bindings:
        raise BatchTrainingError(
            "training plan set domain bindings differ from its authorized inputs"
        )
    configuration_bindings = _context_configuration_bindings(input_refs, participant_ids)
    locator, body_sha256, body_byte_count = store.publish(plan_set)
    identity = {
        key: value
        for key, value in facts.items()
        if key
        not in {
            "analysis_domain_bindings",
            "source_artifact_ref_sha256s",
        }
    }
    identity.update(
        batch_plan_sha256=context.authorization.admission_plan_sha256,
        configuration_binding_set_sha256=_digest(
            [item.to_mapping() for item in configuration_bindings]
        ),
        store_identity_sha256=store.store_identity_sha256,
        store_relative_locator=locator,
        body_sha256=body_sha256,
        body_byte_count=body_byte_count,
    )
    output = BatchArtifactRef(
        artifact_id="training-plan-set-" + plan_set.training_plan_set_sha256[:48],
        artifact_kind="training_plan_set",
        schema_name="mif-training-plan-set",
        schema_version=TrainingPlanSet.schema_version,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=(context.authorization.authorization_sha256),
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=None,
        sha256=None,
        byte_count=None,
        scope=ArtifactScope(
            plan_set.validation_domain_id,
            None,
            None,
            None,
            expected_bindings,
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions={
            "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
        },
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=configuration_bindings,
    )
    try:
        outputs = StageAdapterRegistry((context.adapter_spec,)).validate_outputs(
            context.adapter_spec,
            (output,),
            authorization=context.authorization,
        )
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchTrainingError(
            "training plan set output is not authorized by its context"
        ) from exc
    return BatchNodeExecutionResult(context.authorization.authorization_sha256, outputs)


def _closed_identity(ref: BatchArtifactRef) -> Mapping[str, object]:
    identity = ref.owning_identity
    expected = frozenset(training_plan_set_identity_contract().required_fields)
    if not isinstance(identity, Mapping) or frozenset(identity) != expected:
        raise BatchTrainingError("training plan set compact owner identity is not closed")
    return identity


def _decode_committed_training_plan_set(
    authority: BatchCommittedArtifactAuthority,
    *,
    store: TrainingPlanSetStore,
    allowed_publication_plan_sha256s: tuple[str, ...],
) -> ValidatedTrainingPlanSetProjection:
    """Reopen a body whose publication plan is in authenticated ancestry."""

    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchTrainingError("training plan set decoding requires committed state authority")
    if type(store) is not TrainingPlanSetStore:
        raise BatchTrainingError("training plan set decoding requires its exact owner store")
    try:
        validate_committed_artifact_authority_set(
            (authority,),
            expected_active_plan_sha256=authority.active_plan_sha256,
        )
        ref = validate_artifact_ref(authority.artifact_ref)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchTrainingError(str(exc)) from exc
    contract = training_plan_set_identity_contract()
    if (
        ref.artifact_kind != "training_plan_set"
        or (ref.schema_name, ref.schema_version)
        != (TrainingPlanSet.schema_name, TrainingPlanSet.schema_version)
        or ref.identity_contract != contract
        or ref.owning_identity_contract_sha256 != contract.contract_sha256
        or ref.producer_version != _PRODUCER_VERSION
        or ref.origin is not ArtifactOrigin.INTERNAL_NODE
        or ref.locator is not None
        or ref.sha256 is not None
        or ref.byte_count is not None
        or dict(ref.runtime_versions) != {"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION}
    ):
        raise BatchTrainingError(
            "training plan set ref lacks its exact locatorless durable owner contract"
        )
    if authority.execution_exchange_sha256 is not None:
        raise BatchTrainingError("training plan set publication cannot carry a runtime exchange")
    identity = _closed_identity(ref)
    try:
        batch_plan_sha256 = _sha256(identity["batch_plan_sha256"], "batch_plan_sha256")
        body_sha256 = _sha256(identity["body_sha256"], "body_sha256")
        body_byte_count = _integer(identity["body_byte_count"], "body_byte_count")
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchTrainingError("training plan set compact owner identity is invalid") from exc
    if batch_plan_sha256 not in allowed_publication_plan_sha256s:
        raise BatchTrainingError("training plan set publication plan is not authenticated ancestry")
    relative_locator = identity["store_relative_locator"]
    if identity["store_identity_sha256"] != store.store_identity_sha256:
        raise BatchTrainingError("training plan set locator belongs to a foreign owner store")
    plan_set = store.reopen(
        relative_locator,
        sha256=body_sha256,
        byte_count=body_byte_count,
    )
    facts = _training_plan_set_compact_facts(plan_set)
    configuration_binding_set_sha256 = _digest(
        [item.to_mapping() for item in ref.configuration_bindings]
    )
    expected_identity = {
        key: value
        for key, value in facts.items()
        if key
        not in {
            "analysis_domain_bindings",
            "source_artifact_ref_sha256s",
        }
    }
    expected_identity.update(
        batch_plan_sha256=batch_plan_sha256,
        configuration_binding_set_sha256=configuration_binding_set_sha256,
        store_identity_sha256=store.store_identity_sha256,
        store_relative_locator=relative_locator,
        body_sha256=body_sha256,
        body_byte_count=body_byte_count,
    )
    if _canonical_bytes(_export_json(identity)) != _canonical_bytes(expected_identity):
        raise BatchTrainingError("training plan set compact owner identity differs from its body")
    expected_node_kind = _NODE_KIND_BY_CLASSIFIER[plan_set.classifier_kind]
    if authority.producer_node_kind != expected_node_kind:
        raise BatchTrainingError("training plan set committed producer kind is invalid")
    expected_bindings = facts["analysis_domain_bindings"]
    participant_ids = facts["participant_ids"]
    if (
        ref.artifact_id != "training-plan-set-" + plan_set.training_plan_set_sha256[:48]
        or ref.scope.validation_domain_id != plan_set.validation_domain_id
        or ref.scope.sample_id is not None
        or ref.scope.slide_id is not None
        or ref.scope.project_entry_id is not None
        or ref.scope.analysis_domain_bindings != expected_bindings
        or tuple(item.sample_id for item in ref.configuration_bindings) != participant_ids
    ):
        raise BatchTrainingError("training plan set scope, participants, or bindings differ")
    return _issue_validated_training_plan_set_projection(
        artifact_ref=ref,
        plan_set=plan_set,
        state_head_sha256=authority.state_head_sha256,
        active_plan_sha256=authority.active_plan_sha256,
        terminal_record_sha256=authority.terminal_record_sha256,
        producer_node_kind=authority.producer_node_kind,
        batch_id=facts["batch_id"],
        normalized_manifest_sha256=facts["normalized_manifest_sha256"],
        validation_domain_id=facts["validation_domain_id"],
        classifier_kind=plan_set.classifier_kind,
        participant_ids=participant_ids,
        project_entry_ids=facts["project_entry_ids"],
        configuration_bindings=ref.configuration_bindings,
        analysis_domain_bindings=expected_bindings,
        analysis_domain_binding_set_sha256=facts["analysis_domain_binding_set_sha256"],
        source_artifact_ref_set_sha256=facts["source_artifact_ref_set_sha256"],
        classifier_ids=facts["classifier_ids"],
        classifier_engines=facts["classifier_engines"],
        training_plan_ids=facts["training_plan_ids"],
        training_plan_sha256s=facts["training_plan_sha256s"],
        store_relative_locator=relative_locator,
        body_sha256=body_sha256,
        body_byte_count=body_byte_count,
    )


def decode_committed_training_plan_set(
    authority: BatchCommittedArtifactAuthority,
    *,
    store: TrainingPlanSetStore,
) -> ValidatedTrainingPlanSetProjection:
    """Reopen a plan set published by the authority's active plan itself."""

    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchTrainingError("training plan set decoding requires committed state authority")
    return _decode_committed_training_plan_set(
        authority,
        store=store,
        allowed_publication_plan_sha256s=(authority.active_plan_sha256,),
    )


def decode_current_committed_training_plan_set(
    authority: BatchCommittedArtifactAuthority,
    *,
    store: TrainingPlanSetStore,
    state_store: object,
) -> ValidatedTrainingPlanSetProjection:
    """Reopen an available plan set across authenticated successor revisions."""

    from ..batch_state import BatchStateStore, BatchStateError
    from .stage_adapters import build_training_stage_adapters

    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchTrainingError(
            "current training plan decoding requires committed state authority"
        )
    if type(state_store) is not BatchStateStore:
        raise BatchTrainingError("current training plan decoding requires the exact state owner")
    try:
        validate_committed_artifact_authority_set(
            (authority,),
            expected_active_plan_sha256=authority.active_plan_sha256,
        )
        current = state_store.authenticate_available_artifact(authority.artifact_ref)
        if current != authority:
            raise BatchStateError("training plan set authority is stale for the current head")
        head = state_store.load_head()
        projection = _decode_committed_training_plan_set(
            current,
            store=store,
            allowed_publication_plan_sha256s=head.plan_history_sha256s,
        )
        plan_kind = _NODE_KIND_BY_CLASSIFIER[projection.classifier_kind]
        expected_spec = {item.node_kind: item for item in build_training_stage_adapters()}[
            plan_kind
        ]
        authenticated = state_store.authenticate_available_artifact_producer_spec(
            current.artifact_ref,
            expected_adapter_spec=expected_spec,
        )
        if authenticated != current:
            raise BatchStateError("training plan set producer authority changed while reopening")
    except (
        BatchArtifactError,
        BatchStateError,
        BatchTrainingError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        if isinstance(exc, BatchTrainingError):
            raise
        raise BatchTrainingError("current training plan set authority is not authentic") from exc
    return projection


__all__ = [
    "TrainingPlanSetStore",
    "build_training_plan_set_execution_result",
    "decode_committed_training_plan_set",
    "decode_current_committed_training_plan_set",
    "provision_training_plan_set_store_root",
]
