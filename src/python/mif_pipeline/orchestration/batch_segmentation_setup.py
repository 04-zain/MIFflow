"""Stage-scoped segmentation choices on the existing interaction lineage.

No setup store or inference authority is created here. The batch state owner authenticates
the current node and response before scientific request assembly.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
import hashlib

from mif_pipeline.pipeline_core import AnalysisConfig, InteractionReason, StageResult
from mif_pipeline.pipeline_core.config import SegmentationConfig
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.qupath_runtime.payload_lock import bind_prototype_model_request
from mif_pipeline.qupath_runtime.prerequisite_probe import probe_qupath_prerequisites

from .batch_artifacts import BatchArtifactError, BatchNodeExecutionContext
from .batch_analysis_preflight import resolve_analysis_channels
from .batch_state import BatchStateStore
from .setup.batch_prerequisites import BatchPrerequisiteOwnerInputs

# ==============================================================================
#                            Batch segmentation setup
# ==============================================================================


def probe_selected_segmentation(
    settings: SegmentationConfig,
    owner_inputs: BatchPrerequisiteOwnerInputs,
) -> dict[str, object]:
    """Check the selected model with the existing offline runtime probe owner."""
    if (
        type(settings) is not SegmentationConfig
        or type(owner_inputs) is not BatchPrerequisiteOwnerInputs
    ):
        raise BatchArtifactError("segmentation model check requires typed owner inputs")
    if settings.method != "instanseg":
        from .setup.alternative_segmentation_setup import detector_model_evidence
        try:
            return detector_model_evidence(settings)
        except ValueError as error:
            raise BatchArtifactError(str(error)) from error
    key = (settings.model_path, settings.model_version, False)
    request = owner_inputs.qupath_requests.get(key)
    if request is None:
        runtime = owner_inputs.qupath_requests.get((None, None, False))
        if runtime is None:
            raise BatchArtifactError("segmentation requires reviewed runtime and model inventory")
        try:
            request = bind_prototype_model_request(
                runtime,
                model_root=settings.model_path,
                model_version=settings.model_version,
            )
        except (TypeError, ValueError) as exc:
            raise BatchArtifactError("segmentation model selection is unsupported") from exc
    if (request.model_root, request.model_version, request.require_warpy) != key:
        raise BatchArtifactError("segmentation probe differs from the selected model")
    result = probe_qupath_prerequisites(request)
    for finding in (result.qupath, result.instanseg, result.cpu_engine, result.model):
        if finding is None or finding.status != "ready":
            detail = "model evidence absent" if finding is None else finding.diagnostic
            raise BatchArtifactError(f"segmentation prerequisite is not ready: {detail}")
    identity = {
        "model_path": str(settings.model_path),
        "model_version": settings.model_version,
        "model_files": [
            {
                "relative_path": item.path.relative_to(settings.model_path).as_posix(),
                "sha256": item.sha256,
            }
            for item in sorted(request.model_files, key=lambda value: str(value.path))
        ],
    }
    # The fresh native observation includes a random challenge. Its receipt is
    # useful for readiness, but cannot identify a durable scientific request.
    # Readiness above verifies this exact inventory before we identify it here.
    return {**identity, "evidence_sha256": hashlib.sha256(
        canonical_json_bytes(identity)).hexdigest()}


def _retain_matching_segmentation_request(
    current: Mapping[str, object], *, retained_evidence_sha256: str,
    scientific_request_body_sha256: str, scientific_request_body_byte_count: int,
) -> dict[str, object]:
    """Keep a pending attempt's probe receipt only for identical scientific inputs.

    The caller authenticates the retained envelope and target. This comparison
    changes only the old observation receipt; settings, file hashes, upstream
    identities and every other request field must match its exact saved body.
    """
    evidence = current.get("segmentation_model_evidence")
    if (not isinstance(evidence, Mapping)
            or type(retained_evidence_sha256) is not str
            or len(retained_evidence_sha256) != 64
            or any(c not in "0123456789abcdef" for c in retained_evidence_sha256)):
        raise BatchArtifactError("pending segmentation model evidence is invalid")
    retained = {**current, "segmentation_model_evidence": {
        **evidence, "evidence_sha256": retained_evidence_sha256}}
    encoded = canonical_json_bytes(retained)
    if (hashlib.sha256(encoded).hexdigest() != scientific_request_body_sha256
            or len(encoded) != scientific_request_body_byte_count):
        raise BatchArtifactError("pending segmentation scientific inputs changed")
    return retained


def project_segmentation_setup(
    *,
    config: AnalysisConfig,
    section_id: str,
    node_id: str,
    input_artifact_ref_sha256s: tuple[str, ...],
    validated_response: Mapping[str, object] | None,
    primary_classification_skipped: bool = False,
    channels=None,
    nuclear_channel_id=None,
) -> SegmentationConfig | StageResult:
    """Project an authenticated caller's choices without inspecting a model."""
    if config.segmentation is not None:
        if primary_classification_skipped and config.segmentation.scope != "accepted_tissue":
            raise BatchArtifactError("Without primary classification, segment the accepted analysis regions")
        if validated_response is not None:
            raise BatchArtifactError(
                "stage selection cannot replace configured segmentation settings"
            )
        return config.segmentation
    identity = {
        "node_id": node_id,
        "section_id": section_id,
        "resolved_config_sha256": hashlib.sha256(
            canonical_json_bytes(config.to_mapping())
        ).hexdigest(),
        "input_artifact_ref_sha256s": sorted(input_artifact_ref_sha256s),
    }
    checkpoint_id = (
        "segmentation-setup-" + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:48]
    )
    context = {
        "schema_version": "segmentation-setup-interaction/v1",
        "checkpoint_id": checkpoint_id,
        "section_id": section_id,
    }
    if validated_response is not None:
        supplied_channels = validated_response.get("response", {}).get("channels")
        if supplied_channels is not None:
            context["channels"] = [dict(item) for item in supplied_channels]
        nuclear_channel_id = validated_response.get("response", {}).get("nuclear_channel_id")
    elif channels is not None:
        context["channels"] = [item.to_mapping() for item in channels]
    if nuclear_channel_id is not None:
        context["nuclear_channel_id"] = nuclear_channel_id
    if primary_classification_skipped:
        context["primary_classification_skipped"] = True
    if validated_response is None:
        return StageResult.needs_interaction(
            stage="segmentation",
            slide_id=config.slide_id,
            reason=InteractionReason.SEGMENTATION_SETUP_REQUIRED,
            message="Choose the area to segment and review the tile settings.",
            allowed_actions=("configure_segmentation",),
            resume_context=context,
            checkpoint_id=checkpoint_id,
        )
    if (
        validated_response.get("action") != "configure_segmentation"
        or validated_response.get("interaction_context_sha256")
        != hashlib.sha256(canonical_json_bytes(context)).hexdigest()
    ):
        raise BatchArtifactError(
            "segmentation selection differs from its current section and inputs"
        )
    resume = validated_response.get("response")
    if (
        not isinstance(resume, Mapping)
        or resume.get("action") != "configure_segmentation"
        or any(resume.get(key) != value for key, value in context.items() if key != "channels")
        or ("channels" in context and tuple(resume.get("channels", ())) != tuple(context["channels"]))
    ):
        raise BatchArtifactError("segmentation selection lacks its validated resume context")
    response = resume.get("response")
    if not isinstance(response, Mapping) or set(response) != {"segmentation"}:
        raise BatchArtifactError("segmentation selection has a foreign field inventory")
    selected = SegmentationConfig.from_mapping(response["segmentation"], field="segmentation")
    if selected.method != "instanseg" and "channels" in context:
        if any(index >= len(context["channels"]) for index in selected.parameters["channels"]):
            raise BatchArtifactError("Select channels from this processed image")
    if primary_classification_skipped and selected.scope != "accepted_tissue":
        raise BatchArtifactError("Without primary classification, segment the accepted analysis regions")
    return selected


def resolve_current_segmentation_setup(
    context: BatchNodeExecutionContext,
    *,
    state_store: BatchStateStore,
) -> SegmentationConfig | StageResult:
    """Reopen original admission and current section-bound interaction choices."""
    current = state_store.authenticate_current_execution_context(context)
    snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256)
    nodes = tuple(item for item in snapshot.plan.nodes if item.node_id == context.node_id)
    samples = tuple(
        item
        for item in snapshot.manifest.enabled_samples
        if (item.sample_id,) == current.participant_ids
    )
    if (
        current.node_kind != "segmentation"
        or len(nodes) != 1
        or len(samples) != 1
        or nodes[0].kind != "segmentation"
        or nodes[0].section_target is None
        or nodes[0].participant_ids != current.participant_ids
        or nodes[0].project_entry_ids != current.project_entry_ids
        or nodes[0].analysis_domain_bindings != current.analysis_domain_bindings
        or (nodes[0].section_target.project_entry_id,) != current.project_entry_ids
    ):
        raise BatchArtifactError("segmentation setup differs from its exact current section")
    from .batch_segmentation_artifacts import _segmentation_primary_routes
    _, primary_skipped = _segmentation_primary_routes(context.inputs, current.analysis_domain_bindings)
    if nodes[0].segmentation_settings_revision is not None:
        settings = nodes[0].segmentation_settings_revision.settings
        if primary_skipped and settings.scope != "accepted_tissue":
            raise BatchArtifactError("Without primary classification, segment the accepted analysis regions")
        return settings
    frame = next(item for item in context.inputs if item.artifact_kind == "analysis_frame")
    channels = resolve_analysis_channels(samples[0].config, frame,
        sample_id=samples[0].sample_id, validation_domain_id=current.validation_domain_id)
    from .channel_settings import saved_nuclear_channel_id
    result = project_segmentation_setup(
        config=samples[0].config,
        section_id=nodes[0].section_target.section_id,
        node_id=context.node_id,
        input_artifact_ref_sha256s=tuple(item.artifact_ref_sha256 for item in context.inputs),
        validated_response=state_store.load_validated_interaction_response(context.authorization),
        primary_classification_skipped=primary_skipped,
        channels=channels,
        nuclear_channel_id=saved_nuclear_channel_id(snapshot, sample_id=samples[0].sample_id,
            frame_ref=frame, channels=channels),
    )
    return result
