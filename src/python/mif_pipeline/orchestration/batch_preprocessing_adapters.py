"""Runtime adapter bindings for process-isolated early preprocessing.

The `PreprocessingStageAdapter` record carries `spec`, `node_by_id`, `sample_by_id`,
`response_resolver`, `job_runner`.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .batch_artifacts import (
    BatchArtifactError,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    RuntimeStageAdapter,
    StageAdapterSpec,
)
from .batch_dag import BatchNode, BatchPlan
from .batch_manifest import ResolvedBatchSample
from .batch_preprocessing_contracts import (
    OUTPUT_BY_NODE,
    build_preprocessing_stage_specs,
)
from .batch_preprocessing_execution import decode_preprocessing_job_result
from .batch_preprocessing_support import build_preprocessing_job_request
from .batch_preprocessing_wire import (
    AuthorizedInteractionResponseResolver,
    PreprocessingJobRunner,
)
from .batch_state import BatchRawNodeOutcome
from .batch_state import BatchWorkspaceLease

# ==============================================================================
#                          Batch preprocessing adapters
# ==============================================================================


@dataclass(frozen=True, slots=True)
class PreprocessingStageAdapter:
    """Bind one universal spec to exact plan, sample, and response authority."""

    spec: StageAdapterSpec
    node_by_id: Mapping[str, BatchNode]
    sample_by_id: Mapping[str, ResolvedBatchSample]
    response_resolver: AuthorizedInteractionResponseResolver
    job_runner: PreprocessingJobRunner

    def execute_raw(
        self,
        context: BatchNodeExecutionContext,
        *,
        workspace_lease: BatchWorkspaceLease | None = None,
    ) -> BatchRawNodeOutcome:
        # The service dispatcher supplies its invocation-scoped writer lease to
        # every raw owner.  Preprocessing is process isolated and has no state
        # write path of its own, so it deliberately does not consume the lease.
        del workspace_lease
        if context.adapter_spec != self.spec:
            raise BatchArtifactError("preprocessing adapter received a foreign spec")
        node = self.node_by_id.get(context.node_id)
        if node is None or len(node.participant_ids) != 1:
            raise BatchArtifactError("preprocessing adapter node is absent or non-sample")
        sample = self.sample_by_id.get(node.participant_ids[0])
        if sample is None:
            raise BatchArtifactError("preprocessing adapter sample is absent")
        response = self.response_resolver(context)
        request = build_preprocessing_job_request(node, context, sample, response)
        result = self.job_runner(request)
        return decode_preprocessing_job_result(context, result)

    def execute(self, context: BatchNodeExecutionContext) -> BatchNodeExecutionResult:
        return self.execute_raw(context).execution_result


def build_preprocessing_stage_adapters(
    *,
    plan: BatchPlan,
    resolved_samples: tuple[ResolvedBatchSample, ...],
    response_resolver: AuthorizedInteractionResponseResolver,
    job_runner: PreprocessingJobRunner,
) -> tuple[RuntimeStageAdapter, ...]:
    """Bind universal early-stage specs to one exact plan and sample projection."""

    if not isinstance(plan, BatchPlan):
        raise TypeError("preprocessing adapters require an exact BatchPlan")
    if not isinstance(response_resolver, AuthorizedInteractionResponseResolver):
        raise TypeError("preprocessing adapters require a response resolver")
    if not isinstance(job_runner, PreprocessingJobRunner):
        raise TypeError("preprocessing adapters require a job runner")
    samples = tuple(resolved_samples)
    if not all(isinstance(item, ResolvedBatchSample) for item in samples):
        raise TypeError("preprocessing adapters require resolved samples")
    node_by_id = MappingProxyType(
        {item.node_id: item for item in plan.nodes if item.kind in OUTPUT_BY_NODE}
    )
    sample_by_id = MappingProxyType({item.sample_id: item for item in samples})
    return tuple(
        PreprocessingStageAdapter(
            spec,
            node_by_id,
            sample_by_id,
            response_resolver,
            job_runner,
        )
        for spec in build_preprocessing_stage_specs()
    )


__all__ = ["PreprocessingStageAdapter", "build_preprocessing_stage_adapters"]
