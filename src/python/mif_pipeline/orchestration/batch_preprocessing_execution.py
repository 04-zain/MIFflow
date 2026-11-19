"""Spawn-target dispatch and parent-side decoding for preprocessing jobs.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import hashlib

from mif_pipeline.pipeline_core import StageResult, StageStatus

from .batch_artifacts import (
    BatchArtifactRef,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    StageAdapterRegistry,
)
from .batch_preprocessing_contracts import (
    OPTIONAL_INPUTS,
    REQUIRED_INPUTS,
    preprocessing_identity_contract,
)
from .batch_preprocessing_support import parse_preprocessing_job_request
from .batch_preprocessing_wire import (
    PREPROCESSING_JOB_RESULT_SCHEMA_NAME,
    PREPROCESSING_JOB_SCHEMA_VERSION,
    PreprocessingJobRequest,
    PreprocessingJobResult,
    canonical_bytes,
    canonical_digest,
    decode_json,
    require_text,
)
from .batch_stage1_execution import (
    execute_channel_review,
    execute_inspection,
)
from .batch_reference_unmixing_execution import execute_reference_unmixing
from .batch_state import BatchRawNodeOutcome

# ==============================================================================
#                         Batch preprocessing execution
# ==============================================================================


def run_preprocessing_job(
    request: PreprocessingJobRequest,
) -> PreprocessingJobResult:
    """Top-level spawn target executing exactly one real preprocessing node."""

    node, authorization, sample, inputs, response = parse_preprocessing_job_request(request)
    required_inputs = set(REQUIRED_INPUTS[request.node_kind])
    optional_inputs = set(OPTIONAL_INPUTS[request.node_kind])
    actual_inputs = {item.artifact_kind for item in inputs}
    if not required_inputs <= actual_inputs <= required_inputs | optional_inputs or len(
        inputs
    ) != len(actual_inputs):
        raise ValueError("preprocessing job inputs differ from the closed node contract")
    attempt_id = require_text(authorization.get("attempt_id"), "attempt_id")
    try:
        if request.node_kind == "inspection":
            stage_result, outputs, diagnostics = execute_inspection(request, authorization, sample, response, node=node)
        elif request.node_kind == "channel_review":
            stage_result, outputs, diagnostics = execute_channel_review(
                request, authorization, sample, inputs, response, node=node
            )
        elif request.node_kind == "reference_unmixing":
            stage_result, outputs, diagnostics = execute_reference_unmixing(
                request, authorization, sample, inputs, response
            )
        else:  # pragma: no cover - request constructor has closed kinds
            raise ValueError("unsupported preprocessing node kind")
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        stage_result = StageResult.failed(
            stage=request.node_kind,
            slide_id=require_text(sample.get("slide_id"), "slide_id"),
            error_code=f"preprocessing_{type(exc).__name__.lower()}",
            error_message=message[:4096],
        )
        outputs = ()
        diagnostics = {"exception_type": type(exc).__name__}
    return PreprocessingJobResult(
        schema_name=PREPROCESSING_JOB_RESULT_SCHEMA_NAME,
        schema_version=PREPROCESSING_JOB_SCHEMA_VERSION,
        node_id=request.node_id,
        attempt_id=attempt_id,
        authorization_sha256=request.authorization_sha256,
        stage_result_json=canonical_bytes(stage_result.to_mapping()),
        output_artifact_refs_json=canonical_bytes([item.to_mapping() for item in outputs]),
        diagnostics_json=canonical_bytes(diagnostics),
    )


def decode_preprocessing_job_result(
    context: BatchNodeExecutionContext,
    result: PreprocessingJobResult,
) -> BatchRawNodeOutcome:
    """Reconstruct and validate child output against exact parent authority."""

    if not isinstance(context, BatchNodeExecutionContext) or not isinstance(
        result, PreprocessingJobResult
    ):
        raise TypeError("preprocessing decoder requires typed context and result")
    if (
        result.node_id != context.node_id
        or result.attempt_id != context.authorization.attempt_id
        or result.authorization_sha256 != context.authorization.authorization_sha256
    ):
        raise ValueError("preprocessing result carries foreign attempt authority")
    stage_result = StageResult.from_mapping(
        decode_json(result.stage_result_json, "stage_result_json")
    )
    raw_refs = decode_json(result.output_artifact_refs_json, "output_artifact_refs_json")
    if not isinstance(raw_refs, list):
        raise ValueError("preprocessing result artifacts must be an array")
    contracts = {
        item.artifact_kind: item for item in context.adapter_spec.output_identity_contracts
    }
    outputs = tuple(
        BatchArtifactRef.from_mapping(
            item,
            identity_contract=contracts[item["artifact_kind"]],
        )
        for item in raw_refs
    )
    execution = BatchNodeExecutionResult(
        authorization_sha256=result.authorization_sha256,
        outputs=outputs,
    )
    if stage_result.stage != context.adapter_spec.node_kind:
        raise ValueError("preprocessing StageResult names a foreign stage")
    if stage_result.status is StageStatus.COMPLETED:
        outputs = StageAdapterRegistry((context.adapter_spec,)).validate_outputs(
            context.adapter_spec,
            outputs,
            authorization=context.authorization,
        )
        execution = BatchNodeExecutionResult(result.authorization_sha256, outputs)
    elif outputs:
        raise ValueError("non-completed preprocessing result cannot publish artifacts")
    return BatchRawNodeOutcome(
        context=context,
        stage_result=stage_result,
        execution_result=execution,
        execution_exchange_sha256=canonical_digest(
            {
                "schema_name": result.schema_name,
                "schema_version": result.schema_version,
                "node_id": result.node_id,
                "attempt_id": result.attempt_id,
                "authorization_sha256": result.authorization_sha256,
                "stage_result_sha256": hashlib.sha256(result.stage_result_json).hexdigest(),
                "outputs_sha256": hashlib.sha256(result.output_artifact_refs_json).hexdigest(),
                "diagnostics_sha256": hashlib.sha256(result.diagnostics_json).hexdigest(),
            }
        ),
    )


__all__ = ["decode_preprocessing_job_result", "run_preprocessing_job"]
