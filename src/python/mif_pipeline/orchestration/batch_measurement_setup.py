"""Pure stage-time measurement choices bound to processed channel semantics.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from pathlib import Path

from mif_pipeline.cell_analysis.measurement_setup import (
    configured_measurement_feature_requests,
    validate_measurement_feature_requests,
    validate_segmentation_compartments,
)
from mif_pipeline.pipeline_core import AnalysisConfig, InteractionReason, StageResult, StageStatus
from mif_pipeline.pipeline_core.config import ChannelSpec
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes

from .batch_artifacts import BatchArtifactError, BatchNodeExecutionContext
from .batch_analysis_preflight import resolve_analysis_channels
from .batch_state import BatchStateStore

# ==============================================================================
#                            Batch measurement setup
# ==============================================================================


def _exact_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchArtifactError(f"measurement setup requires exact {field}")
    return value


def _sha256(value: object, field: str) -> str:
    digest = _exact_text(value, field)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise BatchArtifactError(f"measurement setup requires lowercase {field}")
    return digest


def project_measurement_setup(
    *,
    config: AnalysisConfig,
    channels: tuple[ChannelSpec, ...],
    section_id: str,
    node_id: str,
    input_artifact_ref_sha256s: tuple[str, ...],
    validated_response: Mapping[str, object] | None,
    settings_revision=None,
    nuclear_channel_id=None,
    cell_boundary_source=None,
) -> tuple[Mapping[str, object], ...] | StageResult:
    """Project explicit choices or a prompt; authenticate_current stays with the caller."""

    if type(config) is not AnalysisConfig:
        raise BatchArtifactError("measurement setup requires original AnalysisConfig")
    _exact_text(section_id, "section_id")
    _exact_text(node_id, "node_id")
    if not isinstance(channels, tuple) or not 1 <= len(channels) <= 1024:
        raise BatchArtifactError("measurement setup requires processed channels")
    if not all(type(item) is ChannelSpec for item in channels):
        raise BatchArtifactError("measurement setup requires typed processed channels")
    channel_rows = [item.to_mapping() for item in channels]
    channel_ids = [item.channel_id for item in channels]
    if len(set(channel_ids)) != len(channel_ids):
        raise BatchArtifactError("measurement setup processed channel IDs must be unique")
    for channel in channels:
        _exact_text(channel.channel_id, "channel_id")
        _exact_text(channel.name, "channel name")
    if not isinstance(input_artifact_ref_sha256s, tuple) or not input_artifact_ref_sha256s:
        raise BatchArtifactError("measurement setup requires exact input artifacts")
    refs = sorted(_sha256(item, "input artifact digest") for item in input_artifact_ref_sha256s)

    if settings_revision is not None:
        from .batch_measurement_settings import MeasurementSettingsRevision, validate_measurement_settings
        if type(settings_revision) is not MeasurementSettingsRevision:
            raise BatchArtifactError("measurement settings revision must be typed")
        if validated_response is not None:
            raise BatchArtifactError("revised measurements cannot reuse a prior setup response")
        return validate_segmentation_compartments(
            validate_measurement_settings(settings_revision.settings, config=config, channels=channels), cell_boundary_source)

    configured = configured_measurement_feature_requests(config)
    if configured:
        if validated_response is not None:
            raise BatchArtifactError("stage selection cannot replace configured measurements")
        return validate_segmentation_compartments(
            validate_measurement_feature_requests(configured, channels=channels), cell_boundary_source)

    channel_sha256 = hashlib.sha256(canonical_json_bytes(channel_rows)).hexdigest()
    identity = {
        "node_id": node_id,
        "section_id": section_id,
        "resolved_config_sha256": hashlib.sha256(
            canonical_json_bytes(config.to_mapping())
        ).hexdigest(),
        "input_artifact_ref_sha256s": refs,
        "processed_channel_semantics_sha256": channel_sha256,
    }
    checkpoint_id = (
        "measurement-setup-" + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:48]
    )
    context = {
        "schema_version": "measurement-setup-interaction/v1",
        "checkpoint_id": checkpoint_id,
        "section_id": section_id,
        "channels": channel_rows,
    }
    if cell_boundary_source is not None:
        context["cell_boundary_source"] = cell_boundary_source
    if validated_response is not None:
        nuclear_channel_id = validated_response.get("response", {}).get("nuclear_channel_id")
    if nuclear_channel_id is not None:
        if nuclear_channel_id not in channel_ids or channels[channel_ids.index(nuclear_channel_id)].biomarker is not None:
            raise BatchArtifactError("Review the nuclear stain in Channels and markers")
        context["nuclear_channel_id"] = nuclear_channel_id
    if validated_response is None:
        return StageResult.needs_interaction(
            stage="measurements",
            slide_id=config.slide_id,
            reason=InteractionReason.MEASUREMENT_SETUP_REQUIRED,
            message="Choose the channels and exact measurements to extract.",
            allowed_actions=("configure_measurements",),
            resume_context=context,
            checkpoint_id=checkpoint_id,
        )
    if (
        not isinstance(validated_response, Mapping)
        or validated_response.get("action") != "configure_measurements"
        or validated_response.get("interaction_context_sha256")
        != hashlib.sha256(canonical_json_bytes(context)).hexdigest()
    ):
        raise BatchArtifactError(
            "measurement selection differs from its current section and inputs"
        )
    resume = validated_response.get("response")
    expected_resume_fields = {*context, "action", "response", "feature_requests"}
    if (
        not isinstance(resume, Mapping)
        or set(resume) != expected_resume_fields
        or resume.get("action") != "configure_measurements"
        or any(resume.get(key) != value for key, value in context.items() if key != "channels")
        or not isinstance(resume.get("channels"), (list, tuple))
        or tuple(resume["channels"]) != tuple(channel_rows)
    ):
        raise BatchArtifactError("measurement selection lacks its validated resume context")
    response = resume.get("response")
    if (
        not isinstance(response, Mapping)
        or set(response) != {"feature_requests"}
        or resume.get("feature_requests") != response["feature_requests"]
    ):
        raise BatchArtifactError("measurement selection has a foreign field inventory")
    return validate_segmentation_compartments(
        validate_measurement_feature_requests(response["feature_requests"], channels=channels), cell_boundary_source)


def resolve_current_measurement_setup(
    context: BatchNodeExecutionContext,
    *,
    state_store: BatchStateStore,
) -> tuple[Mapping[str, object], ...] | StageResult:
    """Bind the prompt to the current section and authenticated processed frame."""
    current = state_store.authenticate_current_execution_context(context)
    snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256)
    nodes = tuple(item for item in snapshot.plan.nodes if item.node_id == context.node_id)
    samples = tuple(
        item
        for item in snapshot.manifest.enabled_samples
        if (item.sample_id,) == current.participant_ids
    )
    inputs = {item.artifact_kind: item for item in context.inputs}
    if (
        current.node_kind != "measurements"
        or current.node_scope != "sample"
        or len(nodes) != 1
        or len(samples) != 1
        or nodes[0].kind != "measurements"
        or nodes[0].section_target is None
        or nodes[0].participant_ids != current.participant_ids
        or nodes[0].project_entry_ids != current.project_entry_ids
        or nodes[0].analysis_domain_bindings != current.analysis_domain_bindings
        or (nodes[0].section_target.project_entry_id,) != current.project_entry_ids
        or len(context.inputs) != len(inputs)
        or not {"segmentation_result", "analysis_frame"} <= set(inputs) <= {"segmentation_result", "analysis_frame", "segmentation_correction_result"}
        or current.input_artifact_ref_sha256s
        != tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
    ):
        raise BatchArtifactError(
            "measurement setup differs from its exact current section and inputs"
        )
    frame = state_store.authenticate_available_artifact(inputs["analysis_frame"])
    if (
        frame.artifact_ref != inputs["analysis_frame"]
        or frame.producer_node_kind != "analysis_preflight"
        or frame.state_head_sha256 != current.state_head_sha256
        or frame.active_plan_sha256 != current.active_plan_sha256
    ):
        raise BatchArtifactError("measurement setup frame differs from current authority")
    config = samples[0].config
    channels = resolve_analysis_channels(
        config,
        frame.artifact_ref,
        sample_id=samples[0].sample_id,
        validation_domain_id=current.validation_domain_id,
    )
    from .channel_settings import saved_nuclear_channel_id
    from .saved_segmentation_cells import segmentation_cells
    segmentation = state_store.authenticate_available_artifact(inputs["segmentation_result"])
    if "segmentation_correction_result" in inputs:
        from .batch_cell_corrections import decode_committed_cell_correction
        decode_committed_cell_correction(
            state_store.authenticate_available_artifact(inputs["segmentation_correction_result"]), segmentation.artifact_ref)
    boundary_source = segmentation_cells(segmentation, state_store).description["scope"].get("cell_boundary_source")
    result = project_measurement_setup(
        config=config,
        cell_boundary_source=boundary_source,
        nuclear_channel_id=saved_nuclear_channel_id(snapshot, sample_id=samples[0].sample_id,
            frame_ref=frame.artifact_ref, channels=channels),
        channels=channels,
        section_id=nodes[0].section_target.section_id,
        node_id=context.node_id,
        input_artifact_ref_sha256s=current.input_artifact_ref_sha256s,
        validated_response=(None if nodes[0].measurement_settings_revision is not None
            else state_store.load_validated_interaction_response(context.authorization)),
        settings_revision=nodes[0].measurement_settings_revision,
    )
    return result


def reusable_measurement_selection(
    *, snapshot, node, context, segmentation, channels, features, config, state_store,
) -> Mapping[str, object] | None:
    """Select unchanged predecessor columns within an admitted current operation.

    Revision admission already binds the predecessor to this sample and section.
    Retained terminal/compact artifact readers establish its original request;
    it remains history rather than an available graph input. Native admission
    additionally requires these fields to remain selected at the same geometry
    revision. Immutable component parts use the existing downstream data contract.
    """
    revision = node.measurement_settings_revision
    if revision is None:
        return None
    attempts = snapshot.head.attempt_ids_by_node.get(revision.predecessor_node_id, ())
    if not attempts:
        return None
    digest = snapshot.head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1])
    if digest is None:
        return None
    terminal = state_store.load_attempt(digest)
    if terminal.node_id != revision.predecessor_node_id or terminal.stage_result.stage != "measurements":
        raise BatchArtifactError("measurement reuse has a foreign predecessor terminal")
    if terminal.stage_result.status is not StageStatus.COMPLETED:
        return None
    input_digests = tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
    if terminal.execution_context_binding.input_artifact_ref_sha256s != input_digests:
        return None
    from .batch_measurement_artifacts import validate_measurements_result_artifact_ref
    from mif_pipeline.cell_analysis.measurement_rows import (
        MarkerIntensityRowsDescriptor, read_measurement_dataset_selection,
    )
    outputs = terminal.output_artifacts
    if len(outputs) != 1 or outputs[0].artifact_kind != "measurements_result":
        raise BatchArtifactError("measurement reuse lacks its completed predecessor result")
    previous = validate_measurements_result_artifact_ref(outputs[0])
    if (
        previous.sample_id != node.participant_ids[0]
        or previous.slide_id != segmentation.artifact_ref.scope.slide_id
        or previous.project_entry_id != node.section_target.project_entry_id
        or previous.section_id != node.section_target.section_id
        or previous.canonical_image_id != segmentation.canonical_image_id
        or previous.canonical_image_sha256 != segmentation.canonical_image_sha256
        or previous.segmentation_population_id != segmentation.segmentation_population_id
        or (previous.segmentation_generation_id,) != segmentation.generation_ids
        or previous.analysis_domain_bindings != segmentation.analysis_domain_bindings
        or tuple(dict(item) for item in previous.canonical_channels)
        != tuple(item.to_mapping() for item in channels)
        or dict(previous.artifact_ref.runtime_versions) != dict(segmentation.artifact_ref.runtime_versions)
    ):
        return None
    feature_fields = {
        "feature_id", "measurement_name", "kind", "channel_id", "biomarker",
        "compartment", "units", "opt_in",
    }
    prior_by_name = {item["measurement_name"]: item for item in previous.measurement_inventory}
    reusable = []
    versions = set()
    for feature in features:
        prior = prior_by_name.get(feature["measurement_name"])
        if (feature["kind"] in {"expression", "dapi", "autofluorescence"}
                and prior is not None and prior["producer"] == "QuPath"
                and dict(feature) == {key: prior[key] for key in feature_fields}):
            reusable.append(feature["measurement_name"])
            versions.add(prior["producer_version"])
    if not reusable or len(versions) != 1:
        return None
    rows = previous.measurement_rows
    selection = read_measurement_dataset_selection(
        MarkerIntensityRowsDescriptor(rows.path, rows.sha256, rows.byte_count, rows.row_count),
        authenticated_output_root=rows.path.parent.parent.parent,
    )
    if selection["cell_dataset"]["sha256"] != segmentation.cell_evidence_sha256:
        raise BatchArtifactError("measurement reuse selection differs from the accepted cell base")
    return {
        "component": dict(selection["accepted_component"]),
        "geometry_revision": selection["geometry_revision"],
        "measurement_names": reusable,
        "producer_version": next(iter(versions)),
    }


__all__ = ["project_measurement_setup", "resolve_current_measurement_setup", "reusable_measurement_selection"]
