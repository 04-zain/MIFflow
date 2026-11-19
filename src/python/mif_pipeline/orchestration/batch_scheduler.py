"""Admit ready dependency-graph nodes within the configured resource capacities.

The scheduler verifies plan and result inventories, acquires the required resource leases
and dispatches eligible work. Outputs become prerequisites only after their validated
completion is incorporated. Waiting reviews and shared participant barriers keep their own
readiness conditions."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from threading import Condition
from types import MappingProxyType
from uuid import uuid4

from mif_pipeline.orchestration.batch_artifacts import (
    ArtifactOrigin,
    BatchArtifactError,
    BatchArtifactRef,
    BatchAttemptAuthorization,
    BatchNodeExecutionContext,
    StageAdapterRegistry,
    StageAdapterSpec,
    artifact_ref_set_sha256,
    validate_artifact_edge,
    validate_artifact_ref,
)
from mif_pipeline.orchestration.batch_dag import (
    BatchArtifactEdge,
    BatchNode,
    BatchPlan,
    BatchPlanError,
)
from mif_pipeline.orchestration.batch_dispatch import (
    BatchBoundDispatchPreparation,
    BatchDispatchPreparer,
    BatchNodeDispatchGroup,
    BatchNodeDispatcher,
    BatchResourceDemandEstimator,
    partition_dispatch_groups,
    validate_scheduled_dispatch_results,
)
from mif_pipeline.orchestration.batch_manifest import (
    BatchExecutionConfig,
    BatchManifestError,
)
from mif_pipeline.orchestration.batch_resources import (
    BatchResourceDemand,
    BatchSchedulerError,
    ResourceLease,
    ResourceLeasePool,
    estimate_working_set_bytes,
    validate_sha256,
)
from mif_pipeline.orchestration.batch_state import (
    BatchExecutionState,
    BatchAttemptAuthorizationRequest,
    BatchHeadConflictError,
    BatchStateError,
    BatchStateHead,
    BatchStateStore,
    BatchTerminalCommitter,
    BatchWorkspaceLease,
    ScheduledNodeResult,
)
from mif_pipeline.pipeline_core import StageStatus
from mif_pipeline.pipeline_core.execution_resources import operation_budget

# ==============================================================================
#                                Batch scheduler
# ==============================================================================


@dataclass(frozen=True, slots=True, init=False)
class QuiescenceReceipt:
    """Ephemeral assertion issued after scheduler admission and writes stop."""

    scheduler_id: str
    state_head_sha256: str
    admission_stopped: bool
    no_admitted_futures: bool
    no_active_non_cancellable_writes: bool
    _issuer: object = field(repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise BatchSchedulerError("quiescence receipts are issued only by BatchScheduler")

    @classmethod
    def _issue(
        cls, *, scheduler_id: str, state_head_sha256: str, issuer: object
    ) -> "QuiescenceReceipt":
        receipt = object.__new__(cls)
        object.__setattr__(receipt, "scheduler_id", scheduler_id)
        object.__setattr__(
            receipt,
            "state_head_sha256",
            validate_sha256(state_head_sha256, "state_head_sha256"),
        )
        object.__setattr__(receipt, "admission_stopped", True)
        object.__setattr__(receipt, "no_admitted_futures", True)
        object.__setattr__(receipt, "no_active_non_cancellable_writes", True)
        object.__setattr__(receipt, "_issuer", issuer)
        return receipt


@dataclass(slots=True)
class _Candidate:
    node: BatchNode
    spec: StageAdapterSpec
    inputs: tuple[BatchArtifactRef, ...]
    demand: BatchResourceDemand
    resource_lease: ResourceLease
    pending_authorization: BatchAttemptAuthorization | None
    preparation: BatchBoundDispatchPreparation | None = None


@dataclass(frozen=True, slots=True)
class _AdmittedGroup:
    group: BatchNodeDispatchGroup
    resource_leases: tuple[ResourceLease, ...]
    committer: BatchTerminalCommitter


@dataclass(frozen=True, slots=True)
class _PlanEdgeIndexes:
    by_key: Mapping[str, tuple[BatchArtifactEdge, ...]]
    by_consumer: Mapping[str, tuple[BatchArtifactEdge, ...]]
    by_output: Mapping[tuple[str, str, str], tuple[BatchArtifactEdge, ...]]


def _build_edge_indexes(plan: BatchPlan) -> _PlanEdgeIndexes:
    """Build the immutable edge views used throughout one scheduler run."""

    if not isinstance(plan, BatchPlan):
        raise BatchSchedulerError("run_ready requires exact BatchPlan")
    by_key: dict[str, list[BatchArtifactEdge]] = {}
    by_consumer: dict[str, list[BatchArtifactEdge]] = {node.node_id: [] for node in plan.nodes}
    by_output: dict[tuple[str, str, str], list[BatchArtifactEdge]] = {}
    for edge in plan.edges:
        by_key.setdefault(edge.artifact_key, []).append(edge)
        by_consumer.setdefault(edge.consumer_node_id, []).append(edge)
        if edge.producer_node_id is not None:
            by_output.setdefault(
                (
                    edge.producer_node_id,
                    edge.artifact_id,
                    edge.artifact_kind,
                ),
                [],
            ).append(edge)
    return _PlanEdgeIndexes(
        by_key=MappingProxyType({key: tuple(values) for key, values in by_key.items()}),
        by_consumer=MappingProxyType({key: tuple(values) for key, values in by_consumer.items()}),
        by_output=MappingProxyType({key: tuple(values) for key, values in by_output.items()}),
    )


class BatchScheduler:
    """Admit only exact-ready nodes within bounded service-owned capacity."""

    def __init__(
        self,
        config: BatchExecutionConfig,
        state_store: BatchStateStore,
        workspace_lease: BatchWorkspaceLease,
        stage_registry: StageAdapterRegistry,
        dispatch_preparer: BatchDispatchPreparer,
        demand_estimator: BatchResourceDemandEstimator,
        *,
        pending_failure_validator: Callable[[BatchNodeExecutionContext], None] | None = None,
    ) -> None:
        if not isinstance(config, BatchExecutionConfig):
            raise BatchSchedulerError("scheduler requires BatchExecutionConfig")
        if not isinstance(state_store, BatchStateStore):
            raise BatchSchedulerError("scheduler requires BatchStateStore")
        if not isinstance(stage_registry, StageAdapterRegistry):
            raise BatchSchedulerError("scheduler requires exact StageAdapterRegistry")
        if not callable(dispatch_preparer):
            raise BatchSchedulerError("scheduler requires a bound dispatch preparer")
        if not callable(demand_estimator):
            raise BatchSchedulerError("scheduler requires a demand estimator")
        if pending_failure_validator is not None and not callable(pending_failure_validator):
            raise BatchSchedulerError("scheduler pending failure validator must be callable")
        try:
            state_store.validate_writer_lease(workspace_lease)
            replace(config)
            replace(stage_registry)
        except (
            BatchStateError,
            BatchManifestError,
            BatchArtifactError,
            TypeError,
            ValueError,
        ) as exc:
            raise BatchSchedulerError(
                "scheduler workspace lease or injected authority is invalid"
            ) from exc
        self._config = config
        self._state_store = state_store
        self._workspace_lease = workspace_lease
        self._stage_registry = stage_registry
        self._dispatch_preparer = dispatch_preparer
        self._demand_estimator = demand_estimator
        self._pending_failure_validator = pending_failure_validator
        self._resources = ResourceLeasePool(config)
        self._condition = Condition()
        self._accepting = True
        self._run_active = False
        self._active_futures: set[Future[tuple[ScheduledNodeResult, ...]]] = set()
        self._active_non_cancellable_writes = 0
        self._scheduler_id = "scheduler-" + uuid4().hex
        self._receipt_capability = object()
        self._latest_quiescence_receipt: QuiescenceReceipt | None = None
        self._pause_receipt_consumed = False

    def validate_pause_authority(
        self, *, state_store: BatchStateStore, workspace_lease: BatchWorkspaceLease
    ) -> None:
        """Bind a pause operation to this scheduler's exact live authority."""

        if state_store is not self._state_store or workspace_lease is not self._workspace_lease:
            raise BatchSchedulerError("pause scheduler authority is foreign")
        self._state_store.validate_writer_lease(self._workspace_lease)

    @property
    def resource_wait_reasons_by_node(self):
        """Current in-memory admission observations, never a launch authority."""
        return self._resources.wait_reasons_by_node

    def assert_no_admitted_work(self) -> None:
        """Prove this scheduler has no active admitted work before resume."""

        with self._condition:
            if self._run_active or self._active_futures or self._active_non_cancellable_writes:
                raise BatchSchedulerError("scheduler retains admitted work")

    def resolve_current_node_inputs(
        self,
        plan: BatchPlan,
        available_artifacts: Mapping[str, BatchArtifactRef],
        node_id: str,
    ) -> tuple[BatchArtifactRef, ...]:
        """Resolve one current node through the scheduler's canonical edge owner."""

        self._state_store.validate_writer_lease(self._workspace_lease)
        if not isinstance(plan, BatchPlan):
            raise BatchSchedulerError("node inputs require an active plan")
        nodes = tuple(node for node in plan.nodes if node.node_id == node_id)
        if len(nodes) != 1:
            raise BatchSchedulerError("node is absent from the active plan")
        indexes = _build_edge_indexes(plan)
        available = self._validate_plan_and_inventory(
            plan, available_artifacts, self._state_store.load_head(), indexes
        )
        node = nodes[0]
        inputs = self._node_inputs(node, indexes.by_consumer, available, self._resolve_spec(node))
        if inputs is None:
            raise BatchSchedulerError("current node inputs are incomplete")
        return inputs

    def _validate_plan_and_inventory(
        self,
        plan: BatchPlan,
        available_artifacts: Mapping[str, BatchArtifactRef],
        head: BatchStateHead,
        edge_indexes: _PlanEdgeIndexes,
    ) -> dict[str, BatchArtifactRef]:
        if not isinstance(plan, BatchPlan):
            raise BatchSchedulerError("run_ready requires exact BatchPlan")
        try:
            replace(plan)
        except (BatchPlanError, TypeError, ValueError) as exc:
            raise BatchSchedulerError("run_ready plan is not canonical") from exc
        if plan.plan_sha256 != head.active_plan_sha256:
            raise BatchSchedulerError("run_ready plan is not the active state plan")
        if not isinstance(available_artifacts, Mapping):
            raise BatchSchedulerError("available artifacts must be a mapping")
        values: dict[str, BatchArtifactRef] = {}
        try:
            for key, artifact in available_artifacts.items():
                if not isinstance(key, str) or not key:
                    raise BatchSchedulerError(
                        "available artifact edge keys must be non-empty strings"
                    )
                values[key] = validate_artifact_ref(artifact)
        except BatchArtifactError as exc:
            raise BatchSchedulerError(
                "available inventory contains an invalid artifact reference"
            ) from exc
        refs_by_digest: dict[str, BatchArtifactRef] = {}
        for artifact in values.values():
            digest = artifact.artifact_ref_sha256
            prior = refs_by_digest.get(digest)
            if prior is not None and prior != artifact:
                raise BatchSchedulerError("available inventory aliases unequal artifact references")
            refs_by_digest[digest] = artifact
        retained = dict(head.available_artifact_ref_sha256_by_key)
        if any(key != value for key, value in retained.items()):
            raise BatchSchedulerError("state head artifact inventory is not digest-keyed")
        if set(refs_by_digest) != set(retained.values()):
            raise BatchSchedulerError(
                "available artifact values do not equal current head inventory"
            )

        expected_by_key: dict[str, BatchArtifactRef] = {}
        referenced_digests: set[str] = set()

        def bind(key: str, artifact: BatchArtifactRef) -> None:
            prior = expected_by_key.get(key)
            if prior is not None and prior.artifact_ref_sha256 != artifact.artifact_ref_sha256:
                raise BatchSchedulerError("canonical inventory key collision is ambiguous")
            expected_by_key[key] = artifact

        for key, edges in edge_indexes.by_key.items():
            identities = {
                (
                    edge.artifact_id,
                    edge.artifact_kind,
                    edge.producer_node_id,
                    edge.accepted_artifact_ref_sha256,
                )
                for edge in edges
            }
            if len(identities) != 1:
                raise BatchSchedulerError("plan edge key resolves multiple artifact identities")
            edge = edges[0]
            expected_origin = (
                ArtifactOrigin.MANIFEST_EXTERNAL
                if edge.producer_node_id is None
                else ArtifactOrigin.INTERNAL_NODE
            )
            candidates = tuple(
                artifact
                for artifact in refs_by_digest.values()
                if artifact.artifact_id == edge.artifact_id
                and artifact.artifact_kind == edge.artifact_kind
                and artifact.origin is expected_origin
                and artifact.producer_node_id == edge.producer_node_id
                and (
                    edge.accepted_artifact_ref_sha256 is None
                    or artifact.artifact_ref_sha256 == edge.accepted_artifact_ref_sha256
                )
            )
            if len(candidates) > 1:
                raise BatchSchedulerError(
                    "current inventory has ambiguous artifacts for one edge key"
                )
            if not candidates:
                continue
            artifact = candidates[0]
            for typed_edge in edges:
                expected_origin = (
                    ArtifactOrigin.MANIFEST_EXTERNAL
                    if typed_edge.producer_node_id is None
                    else ArtifactOrigin.INTERNAL_NODE
                )
                if (
                    artifact.artifact_id != typed_edge.artifact_id
                    or artifact.artifact_kind != typed_edge.artifact_kind
                    or artifact.origin is not expected_origin
                    or artifact.producer_node_id != typed_edge.producer_node_id
                    or (
                        typed_edge.accepted_artifact_ref_sha256 is not None
                        and artifact.artifact_ref_sha256 != typed_edge.accepted_artifact_ref_sha256
                    )
                ):
                    raise BatchSchedulerError(
                        "available artifact is bound under the wrong edge key"
                    )
                try:
                    validate_artifact_edge(artifact, typed_edge.requirement)
                except BatchArtifactError as exc:
                    raise BatchSchedulerError("available artifact violates its typed edge") from exc
            bind(key, artifact)
            referenced_digests.add(artifact.artifact_ref_sha256)

        for digest, artifact in refs_by_digest.items():
            if digest not in referenced_digests:
                bind(digest, artifact)

        if set(values) != set(expected_by_key):
            raise BatchSchedulerError(
                "available artifact mapping does not use canonical inventory " "edge/fallback keys"
            )
        for key, expected in expected_by_key.items():
            if values[key] != expected:
                raise BatchSchedulerError(
                    "available artifact is bound under the wrong canonical key"
                )
        return values

    def _resolve_spec(self, node: BatchNode) -> StageAdapterSpec:
        try:
            spec = self._stage_registry.resolve(node.kind, node.version)
        except BatchArtifactError as exc:
            raise BatchSchedulerError("active node has no registered adapter") from exc
        if (
            spec.spec_sha256 != node.adapter_spec_sha256
            or spec.resource_kind != node.resource.value
            or spec.dispatch_mode is not node.dispatch_mode
        ):
            raise BatchSchedulerError("registered adapter contradicts exact active node")
        return spec

    def _node_inputs(
        self,
        node: BatchNode,
        edges_by_consumer: Mapping[str, tuple[BatchArtifactEdge, ...]],
        available: Mapping[str, BatchArtifactRef],
        spec: StageAdapterSpec,
    ) -> tuple[BatchArtifactRef, ...] | None:
        edges = edges_by_consumer.get(node.node_id, ())
        if any(edge.artifact_key not in available for edge in edges):
            return None
        values = tuple(available[edge.artifact_key] for edge in edges)
        try:
            validated = self._stage_registry.validate_declared_inputs(spec, values)
        except BatchArtifactError as exc:
            raise BatchSchedulerError("node inputs violate the exact registered adapter") from exc
        if len(validated) != len(edges):
            raise BatchSchedulerError("node input edges are ambiguous")
        return validated

    @staticmethod
    def _dependencies_ready(node: BatchNode, head: BatchStateHead) -> bool:
        return all(
            head.node_status_by_id.get(dependency) is StageStatus.COMPLETED
            and dependency not in head.invalidated_node_ids
            and dependency not in head.pending_authorization_sha256_by_node
            for dependency in node.dependency_ids
        )

    @staticmethod
    def _ordinary_status_ready(node: BatchNode, head: BatchStateHead) -> bool:
        status = head.node_status_by_id.get(node.node_id)
        invalidated = node.node_id in head.invalidated_node_ids
        if status is StageStatus.COMPLETED and not invalidated:
            return False
        if status is StageStatus.FAILED and not invalidated:
            return node.node_id in head.pending_authorization_sha256_by_node
        if status is StageStatus.NEEDS_INTERACTION:
            return node.node_id in head.pending_authorization_sha256_by_node
        return True

    def _validate_demand(
        self,
        node: BatchNode,
        spec: StageAdapterSpec,
        inputs: tuple[BatchArtifactRef, ...],
        demand: object,
    ) -> BatchResourceDemand:
        if not isinstance(demand, BatchResourceDemand):
            raise BatchSchedulerError("demand estimator returned an untyped value")
        if (
            demand.node_id != node.node_id
            or demand.adapter_spec_sha256 != spec.spec_sha256
            or demand.input_artifact_set_sha256 != artifact_ref_set_sha256(inputs)
            or demand.resource_kind is not node.resource
        ):
            raise BatchSchedulerError("resource demand is foreign to the exact node/spec/input set")
        return demand

    @staticmethod
    def _validate_preparation(
        candidate: _Candidate, preparation: object
    ) -> BatchBoundDispatchPreparation:
        if not isinstance(preparation, BatchBoundDispatchPreparation):
            raise BatchSchedulerError("dispatch preparer returned an untyped value")
        if (
            preparation.node_id != candidate.node.node_id
            or preparation.adapter_spec_sha256 != candidate.spec.spec_sha256
            or preparation.input_artifact_set_sha256 != artifact_ref_set_sha256(candidate.inputs)
        ):
            raise BatchSchedulerError(
                "dispatch preparation is foreign to the exact node/spec/input set"
            )
        return preparation

    @staticmethod
    def _context(
        candidate: _Candidate, authorization: BatchAttemptAuthorization
    ) -> BatchNodeExecutionContext:
        binding = authorization.dispatch_binding
        return BatchNodeExecutionContext(
            authorization=authorization,
            node_id=candidate.node.node_id,
            adapter_spec=candidate.spec,
            inputs=candidate.inputs,
            projection_target_sha256=binding.projection_target_sha256,
            current_image_identity_sha256=binding.current_image_identity_sha256,
            script_sha256=binding.script_sha256,
            project_identity_sha256=binding.project_identity_sha256,
            project_authority_head_sha256=binding.project_authority_head_sha256,
            project_qpproj_snapshot_sha256=binding.project_qpproj_snapshot_sha256,
            project_entry_projection_sha256=binding.project_entry_projection_sha256,
            compatibility_signature_sha256=(binding.compatibility_signature_sha256),
        )

    def _admit(
        self,
        *,
        plan: BatchPlan,
        edge_indexes: _PlanEdgeIndexes,
        available: dict[str, BatchArtifactRef],
        attempted_node_ids: set[str],
        dispatch: BatchNodeDispatcher,
        executor: ThreadPoolExecutor,
        futures: dict[Future[tuple[ScheduledNodeResult, ...]], _AdmittedGroup],
        allowed_node_ids: frozenset[str] | None = None,
    ) -> int:
        if not self._accepting:
            return 0
        self._state_store.validate_writer_lease(self._workspace_lease)
        head = self._state_store.load_head()
        if head.execution_state is not BatchExecutionState.ACTIVE:
            raise BatchSchedulerError("scheduler cannot admit a paused batch")
        if head.active_plan_sha256 != plan.plan_sha256:
            if attempted_node_ids and plan.plan_sha256 in head.plan_history_sha256s:
                # Existing contexts remain governed by the store's material
                # descendant checks, but this run never admits stale-plan work.
                return 0
            raise BatchSchedulerError("active plan changed during scheduling")
        selected: list[_Candidate] = []
        try:
            for node in plan.nodes:
                if (
                    (allowed_node_ids is not None and node.node_id not in allowed_node_ids)
                    or node.node_id in attempted_node_ids
                    or not self._ordinary_status_ready(node, head)
                    or not self._dependencies_ready(node, head)
                ):
                    continue
                spec = self._resolve_spec(node)
                inputs = self._node_inputs(node, edge_indexes.by_consumer, available, spec)
                if inputs is None:
                    continue
                demand = self._validate_demand(
                    node, spec, inputs, self._demand_estimator(node, spec, inputs)
                )
                resource_lease = self._resources.try_acquire(demand)
                if resource_lease is None:
                    continue
                selected.append(
                    _Candidate(
                        node=node,
                        spec=spec,
                        inputs=inputs,
                        demand=demand,
                        resource_lease=resource_lease,
                        pending_authorization=None,
                    )
                )
                digest = head.pending_authorization_sha256_by_node.get(node.node_id)
                if digest is not None:
                    selected[-1].pending_authorization = self._state_store.load_authorization(digest)
            if not selected:
                return 0
            replacement_ids = {
                item.node.node_id for item in selected
                if item.pending_authorization is not None
                and self._state_store._pending_response_needs_dispatch_authorization(
                    item.pending_authorization, head)
            }
            for candidate in selected:
                if (candidate.pending_authorization is not None
                        and candidate.node.node_id not in replacement_ids):
                    candidate.pending_authorization = self._state_store.resolve_pending_authorization(
                        candidate.node, head.sha256, self._workspace_lease)
            issuance_candidates = tuple(item for item in selected
                if item.pending_authorization is None or item.node.node_id in replacement_ids)
            for candidate in issuance_candidates:
                candidate.preparation = self._validate_preparation(
                    candidate,
                    self._dispatch_preparer(candidate.node, candidate.spec, candidate.inputs),
                )
            authorizations: tuple[BatchAttemptAuthorization, ...] = ()
            if issuance_candidates:
                requests = tuple(
                    BatchAttemptAuthorizationRequest(
                        node=candidate.node,
                        input_artifacts=candidate.inputs,
                        adapter_spec=candidate.spec,
                        dispatch_authority=candidate.preparation.authority,
                        retry_ordinal=(candidate.pending_authorization.retry_ordinal
                            if candidate.pending_authorization is not None
                            else len(head.attempt_ids_by_node.get(candidate.node.node_id, ()))),
                        interaction_response_sha256=(candidate.pending_authorization.interaction_response_sha256
                            if candidate.pending_authorization is not None else None),
                        interaction_predecessor_terminal_record_sha256=(
                            candidate.pending_authorization.interaction_predecessor_terminal_record_sha256
                            if candidate.pending_authorization is not None else None),
                    )
                    for candidate in issuance_candidates
                    if candidate.preparation is not None
                )
                if len(requests) != len(issuance_candidates):
                    raise BatchSchedulerError("dispatch preparation was incomplete")
                authorizations = self._state_store.authorize_attempts(
                    requests, head.sha256, self._workspace_lease,
                    replaced_pending_authorizations=tuple(
                        item.pending_authorization for item in issuance_candidates
                        if item.pending_authorization is not None),
                )
            new_authorization_by_node = {item.node_id: item for item in authorizations}
            contexts = tuple(
                self._context(
                    candidate,
                    new_authorization_by_node.get(candidate.node.node_id)
                    or candidate.pending_authorization,
                )
                for candidate in selected
            )
            for candidate, context in zip(selected, contexts, strict=True):
                if (
                    candidate.pending_authorization is not None
                    and head.node_status_by_id.get(candidate.node.node_id) is StageStatus.FAILED
                    and candidate.node.node_id not in head.invalidated_node_ids
                    and not context.authorization.was_invalidated_at_admission
                ):
                    if self._pending_failure_validator is None:
                        raise BatchSchedulerError(
                            "failed pending attempt requires a pending failure validator"
                        )
                    self._pending_failure_validator(context)
            lease_by_node = {
                candidate.node.node_id: candidate.resource_lease for candidate in selected
            }
            groups = partition_dispatch_groups(contexts)
            submitted = 0
            for group in groups:
                committer = self._state_store.terminal_committer(
                    group.contexts, self._workspace_lease
                )
                admitted = _AdmittedGroup(
                    group=group,
                    resource_leases=tuple(
                        lease_by_node[context.node_id] for context in group.contexts
                    ),
                    committer=committer,
                )
                def dispatch_admitted(admitted=admitted):
                    demands = tuple(lease.demand for lease in admitted.resource_leases)
                    with operation_budget(
                        sum(item.working_set_bytes for item in demands),
                        sum(item.cpu_threads for item in demands),
                    ):
                        return dispatch(admitted.group.contexts, admitted.committer)
                future = executor.submit(dispatch_admitted)
                futures[future] = admitted
                self._active_futures.add(future)
                self._active_non_cancellable_writes += 1
                attempted_node_ids.update(context.node_id for context in group.contexts)
                submitted += 1
            return submitted
        except BaseException:
            submitted_leases = {
                lease for admitted in futures.values() for lease in admitted.resource_leases
            }
            for candidate in selected:
                if (
                    candidate.resource_lease not in submitted_leases
                    and candidate.resource_lease.is_live
                ):
                    candidate.resource_lease.release()
            raise

    def _incorporate_outputs(
        self,
        edge_indexes: _PlanEdgeIndexes,
        available: dict[str, BatchArtifactRef],
        results: tuple[ScheduledNodeResult, ...],
    ) -> None:
        head = self._state_store.load_head()
        retained_digests = set(head.available_artifact_ref_sha256_by_key.values())
        for key, artifact in tuple(available.items()):
            if artifact.artifact_ref_sha256 not in retained_digests:
                available.pop(key)
        for result in results:
            attempt = self._state_store.load_attempt(result.terminal_attempt_record_sha256)
            for output in attempt.output_artifacts:
                retained = head.available_artifact_ref_sha256_by_key.get(output.artifact_ref_sha256)
                if result.stage_result.status is StageStatus.COMPLETED:
                    if retained != output.artifact_ref_sha256:
                        raise BatchSchedulerError(
                            "completed output is absent from current inventory: "
                            f"output={output.artifact_ref_sha256}, "
                            f"retained={tuple(sorted(head.available_artifact_ref_sha256_by_key))}"
                        )
                    output_edges = edge_indexes.by_output.get(
                        (
                            result.node_id,
                            output.artifact_id,
                            output.artifact_kind,
                        ),
                        (),
                    )
                    output_keys = tuple(edge.artifact_key for edge in output_edges) or (
                        output.artifact_ref_sha256,
                    )
                    for key in output_keys:
                        prior = available.get(key)
                        if (
                            prior is not None
                            and prior.artifact_ref_sha256 != output.artifact_ref_sha256
                        ):
                            raise BatchSchedulerError(
                                "completed output creates an ambiguous inventory key"
                            )
                        available[key] = output
                elif retained == output.artifact_ref_sha256:
                    raise BatchSchedulerError("non-completed output entered current inventory")

    @staticmethod
    def _as_scheduler_error(exc: BaseException) -> BatchSchedulerError:
        if isinstance(exc, BatchSchedulerError):
            return exc
        error = BatchSchedulerError(f"bounded scheduler failed: {exc}")
        error.__cause__ = exc
        return error

    def run_ready(
        self,
        plan: BatchPlan,
        available_artifacts: Mapping[str, BatchArtifactRef],
        dispatch: BatchNodeDispatcher,
        *,
        allowed_node_ids: frozenset[str] | None = None,
    ) -> tuple[ScheduledNodeResult, ...]:
        """Dispatch eligible plan nodes while their prerequisites and resource demands are
        satisfied.

        The scheduler uses the current accepted result inventory to identify ready work,
        then acquires the required capacities for each admitted operation. Completed results
        are incorporated through the service's validation boundary before their successors
        can run. Unresolved reviews and shared-participant dependencies remain pending
        rather than being bypassed. A supplied exact node set limits this wave
        without changing the plan or admitting its newly ready dependants."""

        if not callable(dispatch):
            raise BatchSchedulerError("run_ready requires a node dispatcher")
        edge_indexes = _build_edge_indexes(plan)
        with self._condition:
            if not self._accepting:
                raise BatchSchedulerError("scheduler admission has quiesced")
            if self._run_active:
                raise BatchSchedulerError("scheduler run_ready is already active")
            self._state_store.validate_writer_lease(self._workspace_lease)
            head = self._state_store.load_head()
            if head.execution_state is not BatchExecutionState.ACTIVE:
                raise BatchSchedulerError("scheduler cannot admit a paused batch")
            available = self._validate_plan_and_inventory(
                plan,
                available_artifacts,
                head,
                edge_indexes,
            )
            if allowed_node_ids is not None and (
                type(allowed_node_ids) is not frozenset or not allowed_node_ids
                or not allowed_node_ids <= {node.node_id for node in plan.nodes}
            ):
                raise BatchSchedulerError("selected work must be a nonempty set of current node IDs")
            self._run_active = True
        futures: dict[Future[tuple[ScheduledNodeResult, ...]], _AdmittedGroup] = {}
        attempted_node_ids: set[str] = set()
        committed: list[ScheduledNodeResult] = []
        fatal: BatchSchedulerError | None = None
        executor = ThreadPoolExecutor(
            max_workers=self._config.max_workers,
            thread_name_prefix="mif-batch",
        )
        try:
            while True:
                with self._condition:
                    if fatal is None and self._accepting:
                        try:
                            self._admit(
                                plan=plan,
                                edge_indexes=edge_indexes,
                                available=available,
                                attempted_node_ids=attempted_node_ids,
                                dispatch=dispatch,
                                executor=executor,
                                futures=futures,
                                allowed_node_ids=allowed_node_ids,
                            )
                        except BatchHeadConflictError:
                            continue
                        except BaseException as exc:
                            fatal = self._as_scheduler_error(exc)
                    active = tuple(futures)
                if not active:
                    break
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                done = {future for future in futures if future.done()} or set(done)
                for future in sorted(
                    done,
                    key=lambda item: tuple(
                        context.node_id for context in futures[item].group.contexts
                    ),
                ):
                    admitted = futures.pop(future)
                    try:
                        try:
                            returned = future.result()
                        finally:
                            admitted.committer.seal()
                        exact = validate_scheduled_dispatch_results(
                            returned,
                            committer=admitted.committer,
                            state_store=self._state_store,
                        )
                        self._incorporate_outputs(edge_indexes, available, exact)
                        committed.extend(exact)
                    except BaseException as exc:
                        if fatal is None:
                            fatal = self._as_scheduler_error(exc)
                    finally:
                        for resource_lease in admitted.resource_leases:
                            if resource_lease.is_live:
                                resource_lease.release()
                        with self._condition:
                            self._active_futures.discard(future)
                            self._active_non_cancellable_writes -= 1
                            self._condition.notify_all()
            if fatal is not None:
                raise fatal
            return tuple(
                sorted(
                    committed,
                    key=lambda item: (item.node_id, item.attempt_id),
                )
            )
        finally:
            executor.shutdown(wait=True, cancel_futures=False)
            with self._condition:
                self._run_active = False
                self._condition.notify_all()

    def quiesce(self) -> QuiescenceReceipt:
        with self._condition:
            self._accepting = False
            while self._run_active or self._active_futures or self._active_non_cancellable_writes:
                self._condition.wait()
        try:
            self._state_store.validate_writer_lease(self._workspace_lease)
            head = self._state_store.load_head()
        except BatchStateError as exc:
            raise BatchSchedulerError(
                "cannot issue quiescence from stale scheduler authority"
            ) from exc
        receipt = QuiescenceReceipt._issue(
            scheduler_id=self._scheduler_id,
            state_head_sha256=head.sha256,
            issuer=self._receipt_capability,
        )
        self._latest_quiescence_receipt = receipt
        self._pause_receipt_consumed = False
        return receipt

    def validate_quiescence_receipt(self, receipt: QuiescenceReceipt) -> None:
        if (
            not isinstance(receipt, QuiescenceReceipt)
            or receipt is not self._latest_quiescence_receipt
            or receipt._issuer is not self._receipt_capability
            or receipt.scheduler_id != self._scheduler_id
            or not receipt.admission_stopped
            or not receipt.no_admitted_futures
            or not receipt.no_active_non_cancellable_writes
        ):
            raise BatchSchedulerError("quiescence receipt is foreign or invalid")
        with self._condition:
            if (
                self._accepting
                or self._run_active
                or self._active_futures
                or self._active_non_cancellable_writes
            ):
                raise BatchSchedulerError("scheduler is not currently quiescent")
        try:
            self._state_store.validate_writer_lease(self._workspace_lease)
            head = self._state_store.load_head()
        except BatchStateError as exc:
            raise BatchSchedulerError("quiescence authority is no longer live") from exc
        if head.sha256 != receipt.state_head_sha256:
            raise BatchSchedulerError("quiescence receipt does not bind the current durable head")

    def _consume_quiescence_receipt_for_pause(self, receipt: QuiescenceReceipt) -> object:
        """Return one opaque owner capability for an exact unused pause receipt."""

        self.validate_quiescence_receipt(receipt)
        if self._pause_receipt_consumed:
            raise BatchSchedulerError("quiescence receipt has already authorized pause")
        self._pause_receipt_consumed = True
        return object()


__all__ = [
    "BatchBoundDispatchPreparation",
    "BatchDispatchPreparer",
    "BatchNodeDispatchGroup",
    "BatchNodeDispatcher",
    "BatchResourceDemand",
    "BatchResourceDemandEstimator",
    "BatchScheduler",
    "BatchSchedulerError",
    "QuiescenceReceipt",
    "ResourceLease",
    "ResourceLeasePool",
    "estimate_working_set_bytes",
]
