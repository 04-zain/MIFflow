"""Public facade for batch composition of sample-local preprocessing."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from .batch_preprocessing_adapters import (
    PreprocessingStageAdapter,
    build_preprocessing_stage_adapters,
)
from .batch_preprocessing_contracts import build_preprocessing_stage_specs
from .batch_preprocessing_response_binding import (
    bind_reference_unmixing_response,
    bind_stage1_response,
)
from .batch_preprocessing_execution import (
    decode_preprocessing_job_result,
    run_preprocessing_job,
)
from .batch_reference_unmixing_execution import (
    reference_unmixing_state_to_stage_result,
)
from .batch_stage1_execution import stage1_workflow_to_stage_result
from .batch_preprocessing_support import (
    build_preprocessing_job_request,
    reference_unmixing_start_response_to_request,
)
from .batch_preprocessing_wire import (
    AuthorizedInteractionResponseResolver,
    PREPROCESSING_JOB_REQUEST_SCHEMA_NAME,
    PREPROCESSING_JOB_RESULT_SCHEMA_NAME,
    PreprocessingJobRequest,
    PreprocessingJobResult,
    PreprocessingJobRunner,
    SpawnPreprocessingJobRunner,
)

__all__ = [
    "AuthorizedInteractionResponseResolver",
    "PREPROCESSING_JOB_REQUEST_SCHEMA_NAME",
    "PREPROCESSING_JOB_RESULT_SCHEMA_NAME",
    "PreprocessingJobRequest",
    "PreprocessingJobResult",
    "PreprocessingJobRunner",
    "PreprocessingStageAdapter",
    "SpawnPreprocessingJobRunner",
    "bind_reference_unmixing_response",
    "bind_stage1_response",
    "build_preprocessing_job_request",
    "build_preprocessing_stage_adapters",
    "build_preprocessing_stage_specs",
    "decode_preprocessing_job_result",
    "reference_unmixing_start_response_to_request",
    "reference_unmixing_state_to_stage_result",
    "run_preprocessing_job",
    "stage1_workflow_to_stage_result",
]
