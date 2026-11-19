"""Bound batch dispatch contracts, grouping, and result revalidation.

The `BatchBoundDispatchPreparation` record carries `node_id`, `adapter_spec_sha256`,
`input_artifact_set_sha256`, `authority`.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from mif_pipeline.orchestration.batch_artifacts import (
    BatchArtifactError,
    BatchArtifactRef,
    BatchNodeExecutionContext,
    BatchPreparedDispatchAuthority,
    DispatchMode,
    RuntimeStageAdapterRegistry,
    StageAdapterSpec,
)
from mif_pipeline.orchestration.batch_dag import BatchNode
from mif_pipeline.orchestration.batch_resources import (
    BatchResourceDemand,
    BatchSchedulerError,
    validate_sha256,
)
from mif_pipeline.orchestration.batch_state import (
    BatchStateError,
    BatchStateStore,
    BatchTerminalCommitter,
    ScheduledNodeResult,
)

# ==============================================================================
#                                 Batch dispatch
# ==============================================================================


@dataclass(frozen=True, slots=True)
class BatchBoundDispatchPreparation:
    """Adapter-owned dispatch authority bound to one exact admission input."""

    node_id: str
    adapter_spec_sha256: str
    input_artifact_set_sha256: str
    authority: BatchPreparedDispatchAuthority

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or not self.node_id:
            raise BatchSchedulerError("dispatch preparation requires node_id")
        object.__setattr__(
            self,
            "adapter_spec_sha256",
            validate_sha256(self.adapter_spec_sha256, "adapter_spec_sha256"),
        )
        object.__setattr__(
            self,
            "input_artifact_set_sha256",
            validate_sha256(
                self.input_artifact_set_sha256,
                "input_artifact_set_sha256",
            ),
        )
        if not isinstance(self.authority, BatchPreparedDispatchAuthority):
            raise BatchSchedulerError(
                "dispatch preparation requires BatchPreparedDispatchAuthority"
            )


@runtime_checkable
class BatchDispatchPreparer(Protocol):
    def __call__(
        self,
        node: BatchNode,
        adapter_spec: StageAdapterSpec,
        inputs: tuple[BatchArtifactRef, ...],
    ) -> BatchBoundDispatchPreparation:
        """Prepare executor-owned identities without mutating batch state."""


@runtime_checkable
class BatchResourceDemandEstimator(Protocol):
    def __call__(
        self,
        node: BatchNode,
        adapter_spec: StageAdapterSpec,
        inputs: tuple[BatchArtifactRef, ...],
    ) -> BatchResourceDemand:
        """Return conservative bounded demand for one exact node input set."""


@dataclass(frozen=True, slots=True)
class BatchNodeDispatchGroup:
    """One dispatcher call retaining an independent context per DAG node."""

    contexts: tuple[BatchNodeExecutionContext, ...]
    dispatch_mode: DispatchMode = field(init=False)

    def __post_init__(self) -> None:
        contexts = tuple(self.contexts)
        if not contexts or not all(
            isinstance(item, BatchNodeExecutionContext) for item in contexts
        ):
            raise BatchSchedulerError("dispatch group requires typed execution contexts")
        contexts = tuple(sorted(contexts, key=lambda item: item.node_id))
        mode = contexts[0].adapter_spec.dispatch_mode
        if mode is DispatchMode.SINGLE:
            if len(contexts) != 1:
                raise BatchSchedulerError("single dispatch group must have one context")
        else:
            contexts = tuple(
                sorted(
                    contexts,
                    key=lambda item: (
                        item.authorization.dispatch_binding.project_entry_ids,
                        item.node_id,
                    ),
                )
            )
            first = contexts[0]
            if any(
                item.adapter_spec != first.adapter_spec
                or item.coalescing_key != first.coalescing_key
                for item in contexts[1:]
            ):
                raise BatchSchedulerError("QuPath dispatch group is not exactly compatible")
            entries = tuple(
                entry
                for context in contexts
                for entry in context.authorization.dispatch_binding.project_entry_ids
            )
            if len(entries) != len(set(entries)):
                raise BatchSchedulerError("QuPath dispatch group duplicates a project entry")
            if len(contexts) > 1:
                try:
                    RuntimeStageAdapterRegistry.validate_coalesced_contexts(contexts)
                except BatchArtifactError as exc:
                    raise BatchSchedulerError(
                        "QuPath dispatch group lacks independent targets"
                    ) from exc
        authorities = tuple(item.authorization.authorization_sha256 for item in contexts)
        if len(authorities) != len(set(authorities)):
            raise BatchSchedulerError("dispatch group contexts require independent authorizations")
        object.__setattr__(self, "contexts", contexts)
        object.__setattr__(self, "dispatch_mode", mode)


BatchNodeDispatcher = Callable[
    [tuple[BatchNodeExecutionContext, ...], BatchTerminalCommitter],
    tuple[ScheduledNodeResult, ...],
]


def partition_dispatch_groups(
    contexts: tuple[BatchNodeExecutionContext, ...],
) -> tuple[BatchNodeDispatchGroup, ...]:
    """Partition compatible project contexts and isolate retained-result retries.

    Region and marker retries reopen one original exchange before reuse. Mixing
    them with ordinary work or a retry whose exchange is absent would send the
    whole group through native execution again. Keep those retries independent;
    ordinary work and other stages retain their existing coalescing.
    """

    singles: list[BatchNodeDispatchGroup] = []
    buckets: dict[tuple[object, ...], list[list[BatchNodeExecutionContext]]] = {}
    for context in sorted(contexts, key=lambda item: item.node_id):
        if context.adapter_spec.dispatch_mode is DispatchMode.SINGLE or (
            context.authorization.failed_retry is not None
            and context.adapter_spec.node_kind in {"analysis_domain_selection", "marker_rule_application"}
        ):
            singles.append(BatchNodeDispatchGroup((context,)))
            continue
        key = (context.adapter_spec.spec_sha256, *context.coalescing_key)
        entry_ids = set(context.authorization.dispatch_binding.project_entry_ids)
        compatible = buckets.setdefault(key, [])
        for bucket in compatible:
            occupied = {
                entry
                for item in bucket
                for entry in item.authorization.dispatch_binding.project_entry_ids
            }
            if occupied.isdisjoint(entry_ids):
                bucket.append(context)
                break
        else:
            compatible.append([context])
    grouped = [
        BatchNodeDispatchGroup(tuple(bucket))
        for compatible in buckets.values()
        for bucket in compatible
    ]
    return tuple(
        sorted(
            (*singles, *grouped),
            key=lambda item: tuple(context.node_id for context in item.contexts),
        )
    )


def validate_scheduled_dispatch_results(
    returned: object,
    *,
    committer: BatchTerminalCommitter,
    state_store: BatchStateStore,
) -> tuple[ScheduledNodeResult, ...]:
    """Reopen and revalidate the dispatcher's exact committed result set."""

    if type(returned) is not tuple or not all(
        isinstance(item, ScheduledNodeResult) for item in returned
    ):
        raise BatchSchedulerError("dispatcher did not return typed scheduled results")
    try:
        expected = committer.committed_results
    except BatchStateError as exc:
        raise BatchSchedulerError("dispatcher returned without exact terminal persistence") from exc
    expected_by_key = {
        (item.node_id, item.attempt_id, item.attempt_authorization_sha256): item
        for item in expected
    }
    actual_by_key = {
        (item.node_id, item.attempt_id, item.attempt_authorization_sha256): item
        for item in returned
    }
    if (
        len(expected_by_key) != len(expected)
        or len(actual_by_key) != len(returned)
        or set(actual_by_key) != set(expected_by_key)
        or any(actual_by_key[key] != expected_by_key[key] for key in expected_by_key)
    ):
        raise BatchSchedulerError("dispatcher substituted the exact scheduled result set")
    head = state_store.load_head()
    descendant_digests = {*head.ancestor_head_sha256s, head.sha256}
    for item in expected:
        if item.committed_state_head_sha256 not in descendant_digests:
            raise BatchSchedulerError("scheduled result does not name a committed descendant head")
        if head.terminal_attempt_sha256_by_attempt_id.get(item.attempt_id) != (
            item.terminal_attempt_record_sha256
        ):
            raise BatchSchedulerError("scheduled result is not the retained terminal attempt")
        attempt = state_store.load_attempt(item.terminal_attempt_record_sha256)
        if (
            attempt.node_id != item.node_id
            or attempt.attempt_id != item.attempt_id
            or attempt.authorization_sha256 != item.attempt_authorization_sha256
            or attempt.stage_result != item.stage_result
        ):
            raise BatchSchedulerError("scheduled result contradicts reopened terminal authority")
    return expected


__all__ = [
    "BatchBoundDispatchPreparation",
    "BatchDispatchPreparer",
    "BatchNodeDispatchGroup",
    "BatchNodeDispatcher",
    "BatchResourceDemandEstimator",
]
