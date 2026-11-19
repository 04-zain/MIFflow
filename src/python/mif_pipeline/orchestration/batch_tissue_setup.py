"""Stage-time detector setup through the existing current-image runtime.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from collections.abc import Mapping, Sequence
import hashlib
from pathlib import Path

from mif_pipeline.pipeline_core import AnalysisConfig
from mif_pipeline.pipeline_core.config import ChannelSpec, TissueDetectionParameters
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.qupath_runtime.catalogue_operations import QuPathRunnerProjectOperations
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset
from mif_pipeline.tissue_detection.setup import build_tissue_setup_request

from .batch_artifacts import BatchArtifactError, BatchNodeExecutionContext, StageAdapterSpec
from .batch_analysis_preflight import BatchAnalysisPreflightError, resolve_analysis_channels
from .batch_project import (
    build_project_entry_pyramid_request,
    validate_current_project_entry_pyramid_evidence,
)
from .batch_state import BatchStateStore
from .batch_tissue_setup_artifacts import build_tissue_setup_result_execution_result
from .project_stage_codecs import (
    ProjectStageCodec,
    ProjectStageCodecBinding,
    ProjectStageTargetIntent,
)

# ==============================================================================
#                               Batch tissue setup
# ==============================================================================


def project_tissue_setup_body(
    *,
    config: AnalysisConfig,
    sample_id: str,
    inventory: Sequence[object],
    channels: Sequence[ChannelSpec],
    validated_response: Mapping[str, object] | None,
    review_return: bool = False,
) -> dict[str, object]:
    """Project explicit settings without modifying or granting authority to config."""
    parameters = None if review_return else config.tissue_detection
    source = expected_hash = None
    if validated_response is not None:
        if parameters is not None:
            raise BatchArtifactError("configured tissue setup cannot accept replacement settings")
        action = validated_response.get("action")
        response = validated_response.get("response")
        if action == "refresh_tissue_detectors":
            if not isinstance(response, Mapping) or response:
                raise BatchArtifactError("refreshing tissue detectors requires an empty response")
            return project_tissue_setup_body(
                config=config,
                sample_id=sample_id,
                inventory=inventory,
                channels=channels,
                validated_response=None,
                review_return=review_return,
            )
        if (
            action not in {"select_tissue_detector", "import_tissue_detector"}
            or not isinstance(response, Mapping)
            or set(response) != {"parameters", "source", "expected_classifier_sha256"}
        ):
            raise BatchArtifactError("tissue setup requires its validated selection response")
        parameters = TissueDetectionParameters.from_mapping(
            response["parameters"], field="tissue_detection"
        )
        source = response["source"]
        expected_hash = response["expected_classifier_sha256"]
        if (action == "import_tissue_detector") != (source is not None) or (
            action == "select_tissue_detector"
        ) != (expected_hash is not None):
            raise BatchArtifactError("tissue setup action and source identity differ")
    request = build_tissue_setup_request(
        parameters=parameters,
        channels=channels,
        native_pyramid_inventory=inventory,
        source=source,
        expected_classifier_sha256=expected_hash,
    )
    parameter_mapping = None if parameters is None else parameters.to_mapping()
    if parameter_mapping is not None:
        parameter_mapping.pop("classifier_sha256", None)
    return {
        "schema_version": "roi-workflow-invocation/v1",
        "sample_id": sample_id,
        "tissue_parameters": parameter_mapping,
        "tissue_setup": request,
    }


def build_tissue_setup_stage_codec(
    spec: StageAdapterSpec,
    *,
    state_store: BatchStateStore,
    project_operations: QuPathRunnerProjectOperations,
) -> ProjectStageCodec:
    """Use authenticated current project metadata and the ordinary stage codec."""
    if (
        type(state_store) is not BatchStateStore
        or type(project_operations) is not QuPathRunnerProjectOperations
        or type(spec) is not StageAdapterSpec
        or spec.node_kind != "tissue_setup"
    ):
        raise BatchArtifactError("tissue setup requires its exact composed owners")

    def returning_to_setup(context, execution):
        """Reopen detector choices only for the admitted revision of this task."""
        node = state_store._load_plan_snapshot(execution.active_plan_sha256).node_by_id[context.node_id]
        if "settled_review_revision" not in node:
            return False
        state_store.settled_review_predecessor(context, execution=execution)
        return True

    def projector(
        context: BatchNodeExecutionContext, intent: ProjectStageTargetIntent
    ) -> Mapping[str, object]:
        refs = {item.artifact_kind: item for item in context.inputs}
        if (
            len(context.inputs) != 2
            or set(refs) != {"analysis_frame", "project_catalogue_entry"}
            or intent.prerequisite_artifact_ref_sha256s
            != tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
        ):
            raise BatchArtifactError("tissue setup requires its exact frame and project inputs")
        execution = state_store.authenticate_current_execution_context(context)
        head = state_store.load_head()
        request = build_project_entry_pyramid_request(
            context,
            execution_authority=execution,
            analysis_frame_authority=state_store.authenticate_available_artifact(
                refs["analysis_frame"]
            ),
            project_catalogue_entry_authority=state_store.authenticate_available_artifact(
                refs["project_catalogue_entry"]
            ),
            current_state_head=head,
            state_store=state_store,
            saved_state_before_sha256=intent.saved_state_before_sha256,
        )
        if (
            request.sample_id != intent.sample_id
            or request.slide_id != intent.slide_id
            or request.project_entry_id != intent.project_entry_id
            or request.saved_state_attestation_sha256 != intent.saved_state_before_sha256
        ):
            raise BatchArtifactError("tissue setup target differs from the current image")
        evidence = project_operations.inspect_project_entry_pyramid(
            Path(head.project_path), request
        )
        validate_current_project_entry_pyramid_evidence(
            request, evidence, current_state_head=head, state_store=state_store
        )
        inventory = evidence.native_levels()
        try:
            channels = resolve_analysis_channels(
                intent.resolved_sample.config,
                refs["analysis_frame"],
                sample_id=intent.sample_id,
                validation_domain_id=refs["analysis_frame"].scope.validation_domain_id,
            )
        except BatchAnalysisPreflightError as exc:
            raise BatchArtifactError("tissue setup analysis-frame channels differ") from exc
        response = state_store.load_validated_interaction_response(context.authorization)
        validated_resume = None
        if response is not None:
            action = response.get("action")
            validated_resume = response.get("response")
            if (
                type(action) is not str
                or not isinstance(validated_resume, Mapping)
                or validated_resume.get("action") != action
                or not isinstance(validated_resume.get("response"), Mapping)
            ):
                raise BatchArtifactError(
                    "tissue setup retry response lacks validated action material"
                )
            predecessor = state_store.load_attempt(
                context.authorization.interaction_predecessor_terminal_record_sha256
            )
            prompt = predecessor.stage_result.to_mapping()["resume_context"]
            if (
                not isinstance(prompt, Mapping)
                or prompt.get("schema_version") != "tissue-detector-setup-interaction/v1"
                or canonical_json_bytes(prompt.get("channels"))
                != canonical_json_bytes([channel.to_mapping() for channel in channels])
                or canonical_json_bytes(prompt.get("native_pyramid_inventory"))
                != canonical_json_bytes(inventory)
                or prompt.get("project_path") != head.project_path
                or response.get("interaction_context_sha256")
                != hashlib.sha256(canonical_json_bytes(prompt)).hexdigest()
            ):
                raise BatchArtifactError("tissue setup selection differs from its current prompt")
        body = project_tissue_setup_body(
            config=intent.resolved_sample.config,
            sample_id=intent.sample_id,
            inventory=inventory,
            channels=channels,
            validated_response=validated_resume,
            review_return=returning_to_setup(context, execution),
        )
        if state_store.load_head() != head:
            raise BatchArtifactError("tissue setup state changed during request preparation")
        return body

    def validate_result(evidence):
        execution = state_store.authenticate_current_execution_context(evidence.context)
        return build_tissue_setup_result_execution_result(
            evidence, review_return=returning_to_setup(evidence.context, execution),
        )

    binding = ProjectStageCodecBinding.create(
        adapter_spec_sha256=spec.spec_sha256,
        target_script_asset_id=QuPathScriptAsset.SETUP_TISSUE_DETECTOR.value,
        wire_stage_id="tissue_setup",
        terminal_stage_id="tissue_setup",
        request_codec_version="1",
    )
    return ProjectStageCodec.for_stage_owner(
        binding=binding,
        spec=spec,
        request_projector=projector,
        result_validator=validate_result,
    )
