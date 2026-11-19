"""Issuer-authenticated current execution for aggregate training parents.

The `CurrentTrainingParentExecution` record carries `execution_context_sha256`,
`state_head_sha256`, `active_plan_sha256`, `authorization_sha256`, `node_id` and its
remaining declared fields.

Training records keep subject roles, reference evidence, model settings and application
scope linked. Their validators and native adapters are called by the shared training service
before a fit, evaluation or accepted publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..batch_artifacts import (
    BatchArtifactError,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from ..batch_state import BatchStateError, BatchStateStore
from ._shared import BatchTrainingError, ClassifierKind, _digest
from .planning import ValidatedTrainingPlanSetProjection
from .runtime import (
    TrainingPlanSetStore,
    decode_current_committed_training_plan_set,
)
from .stage_adapters import (
    build_training_stage_adapters,
)

_PARENT_FACTS: Mapping[str, tuple[ClassifierKind, str, str]] = {
    "primary_classifier_training_review": (
        ClassifierKind.PRIMARY_COMPARTMENT,
        "primary_classifier_training_plan_review",
        "primary_training_evidence_snapshot",
    ),
    "secondary_classifier_training_review": (
        ClassifierKind.SECONDARY_STRUCTURE,
        "secondary_classifier_training_plan_review",
        "secondary_training_evidence_snapshot",
    ),
    "supervised_phenotype_training_review": (
        ClassifierKind.SUPERVISED_PHENOTYPE,
        "supervised_phenotype_training_plan_review",
        "supervised_training_evidence_snapshot",
    ),
}
_ISSUER = object()


# ==============================================================================
#                                Parent execution
# ==============================================================================


def _projection_material(
    projection: ValidatedTrainingPlanSetProjection,
) -> dict[str, object]:
    return {
        "artifact_ref_sha256": projection.artifact_ref.artifact_ref_sha256,
        "training_plan_set_sha256": (projection.plan_set.training_plan_set_sha256),
        "state_head_sha256": projection.state_head_sha256,
        "active_plan_sha256": projection.active_plan_sha256,
        "terminal_record_sha256": projection.terminal_record_sha256,
        "producer_node_kind": projection.producer_node_kind,
        "batch_id": projection.batch_id,
        "normalized_manifest_sha256": projection.normalized_manifest_sha256,
        "validation_domain_id": projection.validation_domain_id,
        "classifier_kind": projection.classifier_kind.value,
        "participant_ids": list(projection.participant_ids),
        "project_entry_ids": list(projection.project_entry_ids),
        "configuration_bindings": [item.to_mapping() for item in projection.configuration_bindings],
        "analysis_domain_bindings": [
            item.to_mapping() for item in projection.analysis_domain_bindings
        ],
        "analysis_domain_binding_set_sha256": (projection.analysis_domain_binding_set_sha256),
        "source_artifact_ref_set_sha256": (projection.source_artifact_ref_set_sha256),
        "classifier_ids": list(projection.classifier_ids),
        "classifier_engines": list(projection.classifier_engines),
        "training_plan_ids": list(projection.training_plan_ids),
        "training_plan_sha256s": list(projection.training_plan_sha256s),
        "store_relative_locator": projection.store_relative_locator,
        "body_sha256": projection.body_sha256,
        "body_byte_count": projection.body_byte_count,
    }


def _authority_material(
    *,
    current_execution: BatchCurrentExecutionAuthority,
    training_plan_set: ValidatedTrainingPlanSetProjection,
    evidence_child_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    classifier_kind: ClassifierKind,
) -> dict[str, object]:
    return {
        "current_execution_seal_sha256": current_execution._seal_sha256,
        "execution_context_sha256": current_execution.execution_context_sha256,
        "node_material_sha256": current_execution.node_material_sha256,
        "training_plan_set": _projection_material(training_plan_set),
        "evidence_child_authorities": [
            {
                "artifact_ref_sha256": item.artifact_ref.artifact_ref_sha256,
                "terminal_record_sha256": item.terminal_record_sha256,
                "seal_sha256": item._seal_sha256,
            }
            for item in evidence_child_authorities
        ],
        "classifier_kind": classifier_kind.value,
    }


@dataclass(frozen=True, slots=True, init=False)
class CurrentTrainingParentExecution:
    """Narrowed capability for one exact current Task 11 aggregate parent."""

    execution_context_sha256: str
    state_head_sha256: str
    active_plan_sha256: str
    authorization_sha256: str
    node_id: str
    node_kind: str
    classifier_kind: ClassifierKind
    validation_domain_id: str
    participant_ids: tuple[str, ...]
    project_entry_ids: tuple[str, ...]
    analysis_domain_bindings: tuple[object, ...]
    configuration_bindings: tuple[object, ...]
    training_plan_set: ValidatedTrainingPlanSetProjection
    evidence_child_authorities: tuple[BatchCommittedArtifactAuthority, ...]
    _current_execution: BatchCurrentExecutionAuthority
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("CurrentTrainingParentExecution is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("CurrentTrainingParentExecution cannot be subclassed")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("CurrentTrainingParentExecution is nonserializable")

    def __copy__(self) -> object:
        raise TypeError("CurrentTrainingParentExecution is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("CurrentTrainingParentExecution is noncopyable")


def open_current_training_parent_execution(
    context: BatchNodeExecutionContext,
    *,
    state_store: BatchStateStore,
    plan_set_store: TrainingPlanSetStore,
) -> CurrentTrainingParentExecution:
    """Authenticate and narrow one exact current aggregate-parent execution."""

    if type(context) is not BatchNodeExecutionContext:
        raise BatchTrainingError("parent execution requires an exact context")
    if type(state_store) is not BatchStateStore:
        raise BatchTrainingError("parent execution requires the exact state owner")
    if type(plan_set_store) is not TrainingPlanSetStore:
        raise BatchTrainingError("parent execution requires the exact plan-set owner")
    try:
        current = state_store.authenticate_current_execution_context(context)
        validate_current_execution_authority(current, expected_context=context)
    except (BatchArtifactError, BatchStateError, TypeError, ValueError) as exc:
        raise BatchTrainingError(
            "training parent context is not current and authenticated"
        ) from exc
    facts = _PARENT_FACTS.get(current.node_kind)
    if facts is None:
        raise BatchTrainingError("current execution is not a Task 11 aggregate parent")
    classifier_kind, plan_review_kind, evidence_kind = facts
    production_specs = {spec.node_kind: spec for spec in build_training_stage_adapters()}
    production = production_specs[current.node_kind]
    if (
        context.adapter_spec != production
        or current.node_scope != "validation_domain"
        or current.validation_domain_id is None
        or production.dispatch_mode.value != "single"
        or production.declared_outputs != ("classifier_record_set",)
    ):
        raise BatchTrainingError("training parent lacks its exact production adapter spec")
    try:
        authorities = tuple(
            state_store.authenticate_available_artifact(ref) for ref in context.inputs
        )
        authorities = validate_committed_artifact_authority_set(
            authorities,
            expected_active_plan_sha256=current.active_plan_sha256,
        )
    except (BatchArtifactError, BatchStateError, TypeError, ValueError) as exc:
        raise BatchTrainingError("training parent input authority is not current") from exc
    plan_authorities = tuple(
        item for item in authorities if item.artifact_ref.artifact_kind == "training_plan_set"
    )
    children = tuple(
        item for item in authorities if item.artifact_ref.artifact_kind == evidence_kind
    )
    if (
        len(plan_authorities) != 1
        or len(children) + 1 != len(authorities)
        or plan_authorities[0].producer_node_kind != plan_review_kind
        or any(item.producer_node_kind != evidence_kind for item in children)
    ):
        raise BatchTrainingError(
            "training parent inputs do not contain its exact plan and evidence kinds"
        )
    try:
        plan_authority = state_store.authenticate_available_artifact_producer_spec(
            plan_authorities[0].artifact_ref,
            expected_adapter_spec=production_specs[plan_review_kind],
        )
        authenticated_children = tuple(
            state_store.authenticate_available_artifact_producer_spec(
                child.artifact_ref,
                expected_adapter_spec=production_specs[evidence_kind],
            )
            for child in children
        )
        if plan_authority != plan_authorities[0] or authenticated_children != children:
            raise BatchTrainingError(
                "training parent producer authority changed while authenticating"
            )
        projection = decode_current_committed_training_plan_set(
            plan_authorities[0],
            store=plan_set_store,
            state_store=state_store,
        )
    except (BatchArtifactError, BatchStateError, BatchTrainingError, TypeError, ValueError) as exc:
        raise BatchTrainingError("training parent plan set cannot be reopened") from exc
    head = state_store.load_head()
    if (
        projection.classifier_kind is not classifier_kind
        or projection.batch_id != head.batch_id
        or projection.validation_domain_id != current.validation_domain_id
        or projection.participant_ids != current.participant_ids
        or projection.project_entry_ids != current.project_entry_ids
        or projection.analysis_domain_bindings != current.analysis_domain_bindings
        or projection.configuration_bindings != current.configuration_bindings
    ):
        raise BatchTrainingError("training parent material differs from its authenticated plan set")
    expected_entry_identity: dict[str, tuple[str, str]] = {}
    for plan in projection.plan_set.plans:
        for entry in plan.request.entries:
            identity = (entry.sample_id, entry.slide_id)
            previous = expected_entry_identity.setdefault(entry.project_entry_id, identity)
            if previous != identity:
                raise BatchTrainingError("training plan set has inconsistent entry identity")
    expected_bindings = {
        entry_id: tuple(
            item
            for item in projection.analysis_domain_bindings
            if item.project_entry_id == entry_id
        )
        for entry_id in projection.project_entry_ids
    }
    expected_configurations = {item.sample_id: item for item in current.configuration_bindings}
    child_entries: dict[str, BatchCommittedArtifactAuthority] = {}
    for child in children:
        ref = child.artifact_ref
        entry_id = ref.scope.project_entry_id
        entry_identity = expected_entry_identity.get(entry_id or "")
        if (
            entry_id is None
            or entry_identity is None
            or entry_id in child_entries
            or ref.scope.validation_domain_id != current.validation_domain_id
            or (ref.scope.sample_id, ref.scope.slide_id) != entry_identity
            or ref.configuration_bindings != (expected_configurations[entry_identity[0]],)
            or ref.scope.analysis_domain_bindings != expected_bindings[entry_id]
            or ref.owning_identity.get("training_plan_set_sha256")
            != projection.plan_set.training_plan_set_sha256
            or ref.owning_identity.get("project_entry_id") != entry_id
            or ref.owning_identity.get("sample_id") != entry_identity[0]
            or ref.owning_identity.get("slide_id") != entry_identity[1]
        ):
            raise BatchTrainingError("training parent evidence-child authority is inconsistent")
        child_entries[entry_id] = child
    if set(child_entries) != set(expected_entry_identity):
        raise BatchTrainingError("training parent lacks its complete evidence-child inventory")
    ordered_children = tuple(child_entries[entry_id] for entry_id in sorted(expected_entry_identity))
    material = _authority_material(
        current_execution=current,
        training_plan_set=projection,
        evidence_child_authorities=ordered_children,
        classifier_kind=classifier_kind,
    )
    values = {
        "execution_context_sha256": current.execution_context_sha256,
        "state_head_sha256": current.state_head_sha256,
        "active_plan_sha256": current.active_plan_sha256,
        "authorization_sha256": current.authorization_sha256,
        "node_id": current.node_id,
        "node_kind": current.node_kind,
        "classifier_kind": classifier_kind,
        "validation_domain_id": current.validation_domain_id,
        "participant_ids": current.participant_ids,
        "project_entry_ids": current.project_entry_ids,
        "analysis_domain_bindings": current.analysis_domain_bindings,
        "configuration_bindings": current.configuration_bindings,
        "training_plan_set": projection,
        "evidence_child_authorities": ordered_children,
        "_current_execution": current,
        "_seal_sha256": _digest(material),
        "_issuer": _ISSUER,
    }
    result = object.__new__(CurrentTrainingParentExecution)
    for name in CurrentTrainingParentExecution.__dataclass_fields__:
        object.__setattr__(result, name, values[name])
    return validate_current_training_parent_execution(
        result,
        expected_context=context,
        state_store=state_store,
    )


def validate_current_training_parent_execution(
    authority: CurrentTrainingParentExecution,
    *,
    expected_context: BatchNodeExecutionContext,
    state_store: BatchStateStore | None = None,
) -> CurrentTrainingParentExecution:
    """Validate the issuer seal and its exact scheduler context binding.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if type(authority) is not CurrentTrainingParentExecution or authority._issuer is not _ISSUER:
        raise BatchTrainingError("training parent execution issuer is invalid")
    try:
        validate_current_execution_authority(
            authority._current_execution, expected_context=expected_context
        )
        if state_store is not None:
            if type(state_store) is not BatchStateStore:
                raise TypeError("state_store must be the exact state owner")
            current = state_store.authenticate_current_execution_context(expected_context)
            if current != authority._current_execution:
                raise BatchTrainingError("training parent execution is no longer current")
        validate_committed_artifact_authority_set(
            authority.evidence_child_authorities,
            expected_active_plan_sha256=authority.active_plan_sha256,
        )
    except (
        BatchArtifactError,
        BatchStateError,
        BatchTrainingError,
        TypeError,
        ValueError,
    ) as exc:
        raise BatchTrainingError("training parent execution authority is invalid") from exc
    material = _authority_material(
        current_execution=authority._current_execution,
        training_plan_set=authority.training_plan_set,
        evidence_child_authorities=authority.evidence_child_authorities,
        classifier_kind=authority.classifier_kind,
    )
    current = authority._current_execution
    if (
        authority._seal_sha256 != _digest(material)
        or authority.execution_context_sha256 != current.execution_context_sha256
        or authority.state_head_sha256 != current.state_head_sha256
        or authority.active_plan_sha256 != current.active_plan_sha256
        or authority.authorization_sha256 != current.authorization_sha256
        or authority.node_id != current.node_id
        or authority.node_kind != current.node_kind
        or authority.validation_domain_id != current.validation_domain_id
        or authority.participant_ids != current.participant_ids
        or authority.project_entry_ids != current.project_entry_ids
        or authority.analysis_domain_bindings != current.analysis_domain_bindings
        or authority.configuration_bindings != current.configuration_bindings
    ):
        raise BatchTrainingError("training parent execution seal is invalid")
    return authority


__all__ = [
    "CurrentTrainingParentExecution",
    "open_current_training_parent_execution",
    "validate_current_training_parent_execution",
]
