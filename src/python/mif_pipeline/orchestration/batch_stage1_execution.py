"""Inspection and channel-review services.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from ome_types import from_xml, to_xml

from mif_pipeline.io import (
    CanonicalIngestionError,
    InspectedSourceAuthority,
    open_image,
    resolve_canonical_image_selection,
    resolve_component_identity_inventory,
)
from mif_pipeline.pipeline_core import (
    InteractionAction,
    InteractionReason,
    StageResult,
)
from mif_pipeline.preprocessing import (
    ReviewedPanelMapping,
    Stage1Workflow,
    apply_stage1_biomarker_values,
    apply_stage1_channel_name_replacement,
    apply_reviewed_panel_mapping,
    inspect_opened_wsi,
    stage1_workflow_from_inspection,
)
from mif_pipeline.preprocessing.inspection.workflow import (
    apply_stage1_channel_content, apply_stage1_component_values, apply_stage1_channel_cycles,
)

from .batch_artifacts import BatchArtifactRef
from .batch_preprocessing_support import (
    artifact_path,
    artifact_ref,
    equivalence,
    file_sha256,
    package_version,
    publish_exact,
    response_body,
    runtime_versions,
    source_path,
    unmixing_source_state,
)
from .batch_preprocessing_reuse import load_reusable_preprocessing_input
from .batch_preprocessing_wire import (
    PreprocessingJobRequest,
    canonical_bytes,
    canonical_digest,
    require_sha256,
    require_text,
)

STAGE1_INTERACTION_VERSION = "mif-batch-stage1-interaction/v1"


# ==============================================================================
#                             Batch stage1 execution
# ==============================================================================


def _validate_source_selection(
    authority: InspectedSourceAuthority,
    sample: Mapping[str, object],
    *,
    allow_pending_organization: bool = False,
) -> None:
    """Check declared selection through the IO owner's analytical predicates."""
    from mif_pipeline.io.acquired_plane_view import is_acquired_plane_view, read_plane_descriptor
    if is_acquired_plane_view(authority.source_identity.resolved_path):
        parent, record, projected, _ = read_plane_descriptor(authority.source_identity.resolved_path,
            inspection=authority)
        if projected.model_dump(mode="json") != authority.canonical_ome.model_dump(mode="json"):
            raise ValueError("Completed plane-view metadata differs from its descriptor")
        selected = parent.series[parent.selection.source_series_index]
        expected_series = (replace(selected, index=0, resolutions=tuple(replace(level,
            size_c=len(record['rows']), size_t=1, size_z=1, dimension_order='XYCZT')
            for level in selected.resolutions)),)
        if authority.series != expected_series or authority.backend_versions != replace(parent.backend_versions,
                backend='mif-acquired-plane-view/v1'):
            raise ValueError("Completed plane-view reader inventory differs from its parent")
        expected = resolve_canonical_image_selection(projected, authority.series,
            image_index=0, series_index=0, require_planar_channels=True)
        if authority.selection != expected:
            raise ValueError("Completed plane-view selection differs from its planar inventory")
        _validate_source_selection(parent, sample, allow_pending_organization=True)
        return
    if sample.get('source_mif_path') is not None and source_path(sample) != authority.source_identity.resolved_path:
        raise ValueError("source inspection path differs from declared original source")
    declared_hash = sample.get("source_file_sha256")
    if (
        declared_hash is not None
        and require_sha256(declared_hash, "source_file_sha256")
        != authority.source_identity.file_sha256
    ):
        raise ValueError("source file SHA-256 differs from declared selection")
    try:
        expected = resolve_canonical_image_selection(
            authority.canonical_ome,
            authority.series,
            image_index=sample.get("source_image_index"),
            series_index=sample.get("source_series_index"),
            require_planar_channels=True,
            allow_pending_organization=allow_pending_organization,
        )
    except CanonicalIngestionError as exc:
        raise ValueError(f"source inspection selection is invalid: {exc}") from exc
    if authority.selection != expected:
        raise ValueError("source inspection selection differs from declared source selection")


def _selection_identity(authority: InspectedSourceAuthority) -> dict[str, object]:
    selection = authority.selection
    return {
        "image_id": selection.image_id,
        "pixels_id": selection.pixels_id,
        "source_image_index": selection.source_image_index,
        "source_series_index": selection.source_series_index,
        "series_name": selection.series_name,
    }


def _channel_inventory(ome) -> list[dict[str, object]]:
    return [
        {
            "id": channel.id,
            "name": channel.name,
            "fluor": channel.fluor,
        }
        for channel in ome.images[0].pixels.channels
    ]


def execute_inspection(
    request: PreprocessingJobRequest,
    authorization: Mapping[str, object],
    sample: Mapping[str, object],
    response: Mapping[str, object] | None = None,
    *, node: Mapping[str, object] | None = None,
) -> tuple[StageResult, tuple[BatchArtifactRef, ...], dict[str, object]]:
    """Complete original inspection only after its cycle organization is reviewed."""
    from .batch_acquisition_organization import (pending_inspection_path, load_pending_inspection,
        requires_organization, organization_stage_result, validate_organization_response,
        organization_seed, REVISION_DISPATCH_VERSION)
    from mif_pipeline.io.acquired_plane_view import write_acquired_plane_view
    previous = None
    if response is not None and response.get("schema_version") == REVISION_DISPATCH_VERSION:
        from .batch_preprocessing_contracts import preprocessing_identity_contract
        if set(response) != {"schema_version", "previous_inspection_artifact", "accepted_response"}:
            raise ValueError("Inspection revision dispatch fields differ")
        previous_ref = BatchArtifactRef.from_mapping(response["previous_inspection_artifact"],
            identity_contract=preprocessing_identity_contract("source_inspection"))
        revision = (node or {}).get("settled_review_revision")
        if revision is None or previous_ref.producer_node_id != revision["predecessor_node_id"]:
            raise ValueError("Inspection revision source is foreign")
        previous = _load_authority(previous_ref)
        _validate_source_selection(previous, sample)
        response = response["accepted_response"]
    pending_path = pending_inspection_path(sample, request.node_id)
    body = response_body(response)
    if pending_path.exists():
        authority, pending_sha = load_pending_inspection(sample, request.node_id)
        workflow = None
    else:
        if body is not None:
            raise ValueError("Cycle-review response has no retained original inspection")
        source_file = source_path(sample)
        with open_image(source_file) as source:
            workflow, authority = inspect_opened_wsi(source_file, source,
                image_index=sample.get("source_image_index"), series_index=sample.get("source_series_index"),
                allow_pending_organization=True)
            _validate_source_selection(authority, sample, allow_pending_organization=True)
            if workflow.selection != authority.selection or authority.source_identity != source.identity:
                raise ValueError("Inspection authority differs from the opened source selection")
        pending_sha = None
    if requires_organization(authority) or pending_sha is not None or (node or {}).get("settled_review_revision") is not None:
        if pending_sha is None:
            pending_bytes = authority.to_json_bytes()
            publish_exact(pending_path, pending_bytes)
            pending_sha = hashlib.sha256(pending_bytes).hexdigest()
        interaction = organization_stage_result(authority, slide_id=sample["slide_id"],
            node_id=request.node_id, pending_sha256=pending_sha, seed=organization_seed(authority, previous))
        if body is None:
            return interaction, (), {"pending_source_inspection_sha256": pending_sha}
        rows, decision = validate_organization_response(authority, interaction, body)
        descriptor = write_acquired_plane_view(artifact_path(sample,
            f"inspection/{request.node_id}/acquired-planes.mif-plane-view.json"), authority, rows, decision)
        with open_image(descriptor) as source:
            # Probe every addressed plane before completion. Reader failures remain
            # an unfinished inspection and cannot publish a source authority.
            from mif_pipeline.io import PlaneIndex, Region
            for level in source.series[0].resolutions:
                for channel in range(level.size_c):
                    source.read_region(series=0, resolution=level.index,
                        plane=PlaneIndex(c=channel), region=Region(0, 0, 1, 1))
            workflow, authority = inspect_opened_wsi(descriptor, source, image_index=0, series_index=0)
        _validate_source_selection(authority, sample)
    elif body is not None:
        raise ValueError("This noncyclic inspection has no cycle-review response")
    locator = artifact_path(sample, (f"inspection/revisions/{request.node_id}/source-inspection.json"
        if (node or {}).get("settled_review_revision") is not None else "inspection/source-inspection.json"))
    payload = authority.to_json_bytes()
    publish_exact(locator, payload)
    source_identity = authority.source_identity
    selection = authority.selection
    series_material = [
        (
            _series.to_mapping()
            if hasattr(_series, "to_mapping")
            else {
                "index": _series.index,
                "name": _series.name,
                "levels": [
                    [level.index, level.size_x, level.size_y, level.size_c, level.dtype.str]
                    for level in _series.resolutions
                ],
            }
        )
        for _series in authority.series
    ]
    channels = _channel_inventory(workflow.metadata)
    versions = runtime_versions(
        bioformats=authority.backend_versions.bioformats_runtime_version,
        bffile=authority.backend_versions.bffile_version,
    )
    canonical = {
        "source_sha256": source_identity.file_sha256,
        "image_id": selection.image_id,
        "pixels_id": selection.pixels_id,
    }
    identity = {
        "equivalence": equivalence(
            canonical=canonical,
            inventory={"series": series_material, "channels": channels},
            scientific={
                "ome_sha256": hashlib.sha256(authority.canonical_ome_xml.encode()).hexdigest()
            },
            qc={"authority_sha256": authority.content_sha256},
            configuration_sha256=request.resolved_config_sha256,
            runtime_versions_value=versions,
        ),
        "authority_sha256": authority.content_sha256,
        "source": {
            "file_sha256": source_identity.file_sha256,
            "size_bytes": source_identity.size_bytes,
            "modified_time_ns": source_identity.modified_time_ns,
            "device_id": source_identity.device_id,
            "inode_id": source_identity.inode_id,
        },
        "selection": _selection_identity(authority),
        "ome_sha256": hashlib.sha256(authority.canonical_ome_xml.encode()).hexdigest(),
        "series_inventory_sha256": canonical_digest(series_material),
        "channel_inventory_sha256": canonical_digest(channels),
    }
    ref = artifact_ref(
        request=request,
        authorization=authorization,
        sample=sample,
        artifact_kind="source_inspection",
        locator=locator,
        file_sha256_value=hashlib.sha256(payload).hexdigest(),
        byte_count=len(payload),
        owning_identity=identity,
        runtime_versions_value=versions,
        compatibility_material={
            "source": canonical,
            "series": series_material,
            "channels": channels,
        },
    )
    return (
        StageResult.completed(stage="inspection", slide_id=sample["slide_id"]),
        (ref,),
        {"opened_source_count": 1, "source_hash_count": 1},
    )


def _load_authority(ref: BatchArtifactRef) -> InspectedSourceAuthority:
    if ref.locator is None or ref.sha256 is None or ref.byte_count is None:
        raise ValueError("source inspection artifact is not file-backed")
    payload = ref.locator.read_bytes()
    if len(payload) != ref.byte_count or hashlib.sha256(payload).hexdigest() != ref.sha256:
        raise ValueError("source inspection artifact bytes changed")
    authority = InspectedSourceAuthority.from_json_bytes(payload)
    if authority.content_sha256 != ref.owning_identity["authority_sha256"]:
        raise ValueError("source inspection authority digest changed")
    if ref.owning_identity["selection"] != _selection_identity(authority):
        raise ValueError("source inspection artifact selection differs from authority")
    return authority


def _stage1_rows(workflow: Stage1Workflow) -> list[dict[str, object]]:
    if len(workflow.biomarker_rows) > 512:
        raise ValueError("stage-one channel review exceeds bounded interaction size")
    return [
        {
            "row_id": f"{row.image_index}:{row.channel_index}",
            "image_index": row.image_index,
            "channel_index": row.channel_index,
            "channel_id": row.channel_id,
            "channel_name": row.channel_name,
            "fluorophore": row.fluorophore,
            "component_name": row.component_name,
            "assignment": row.biomarker_assignment,
            "biomarker": row.biomarker,
        }
        for row in workflow.biomarker_rows
    ]


def stage1_workflow_to_stage_result(
    workflow: Stage1Workflow,
    *,
    slide_id: str,
    inspection_artifact_ref_sha256: str,
    inspection_authority_sha256: str,
) -> StageResult:
    """Translate Stage1Workflow into bounded canonical batch control state."""

    if workflow.status == "ready":
        return StageResult.completed(stage="channel_review", slide_id=slide_id)
    rows = _stage1_rows(workflow)
    from mif_pipeline.io.metadata.calibration import recorded_autofluorescence_subtracted
    from mif_pipeline.io.metadata.canonical import resolve_component_identity_inventory
    inventory = resolve_component_identity_inventory(workflow.metadata, 0)
    source_state = unmixing_source_state(workflow.metadata)[0]
    nuclear = None
    if inventory is not None and inventory.nuclear_component_index is not None:
        nuclear = (inventory.component_names[inventory.nuclear_component_index]
            if unmixing_source_state(workflow.metadata)[0] == "raw"
            else inventory.channel_ids[inventory.nuclear_component_index])
    service_context = {
        "schema_version": STAGE1_INTERACTION_VERSION,
        "inspection_artifact_ref_sha256": require_sha256(
            inspection_artifact_ref_sha256, "inspection artifact ref"
        ),
        "inspection_authority_sha256": require_sha256(
            inspection_authority_sha256, "inspection authority"
        ),
        "workflow_status": workflow.status,
        "source_spectral_state": source_state,
        "source_image_index": workflow.selection.source_image_index,
        "source_series_index": workflow.selection.source_series_index,
        "source_image_id": workflow.selection.image_id,
        "source_pixels_id": workflow.selection.pixels_id,
        "source_series_name": workflow.selection.series_name,
        "channel_review_rows": rows,
        "nuclear_stain": nuclear,
        "nuclear_stain_reviewed": inventory is not None and inventory.nuclear_stain_reviewed,
        "component_af_retained": recorded_autofluorescence_subtracted(workflow.metadata) is not True,
        "component_review_rows": [{"component_name": r.component_name, "assignment": r.assignment, "biomarker": r.biomarker} for r in workflow.component_rows],
        "replace_channel_names_with_biomarkers": (workflow.replace_channel_names_with_biomarkers),
        "checkpoint_schema_version": 2,
        "response_schema_version": "mif-batch-stage1-response/v1",
    }
    from mif_pipeline.io.metadata.cycles import resolve_channel_cycles, resolve_source_channel_cycles
    cycles = resolve_channel_cycles(workflow.metadata)
    source_cycles = resolve_source_channel_cycles(workflow.metadata)
    service_context["channel_cycle_values"] = ([] if cycles is None else [
        dict(channel_id=row.channel_id, cycle=cycle)
        for row, cycle in zip(workflow.biomarker_rows, cycles, strict=True)])
    service_context["source_channel_cycles"] = list(source_cycles) if source_cycles is not None else None
    if cycles is not None:
        service_context["component_cycles"] = sorted({cycle for cycle in cycles if cycle is not None})
    checkpoint_id = canonical_digest(service_context)
    message = (
        "Review channel labels and marker assignments; specify output components for raw spectral bands."
        if workflow.status == "needs_biomarkers"
        else "Choose whether reviewed biomarkers replace slide-wide channel names."
    )
    return StageResult.needs_interaction(
        stage="channel_review",
        slide_id=slide_id,
        reason=InteractionReason.OTHER_EXPLICIT_REASON,
        message=message,
        allowed_actions=(InteractionAction.ACCEPT.value,),
        resume_context={
            "schema_version": STAGE1_INTERACTION_VERSION,
            "service_context": service_context,
        },
        checkpoint_id=checkpoint_id,
    )


def _apply_stage1_response(
    workflow: Stage1Workflow,
    response: Mapping[str, object],
    *,
    authority: InspectedSourceAuthority,
) -> Stage1Workflow:
    expected = {
        "schema_version",
        "workflow_status",
        "biomarker_values",
        "component_values",
        "replace_channel_names_with_biomarkers",
        "channel_content",
        "nuclear_stain",
    }
    if (
        set(response) - {"channel_cycle_values"} != expected
        or response.get("schema_version") != "mif-batch-stage1-response/v1"
    ):
        raise ValueError("stage-one response fields or schema differ")
    if response["workflow_status"] != workflow.status:
        raise ValueError("stage-one response is stale for workflow status")
    # Saved review metadata supplies editable defaults only. Apply the submitted
    # choices to the inspected source so its original display names can be restored.
    workflow = replace(stage1_workflow_from_inspection(authority), status=workflow.status)
    biomarker_values = response["biomarker_values"]
    replacement = response["replace_channel_names_with_biomarkers"]
    content = response["channel_content"]
    if content not in {"raw", "unmixed"}:
        raise ValueError("Choose raw measurement bands or separated components")
    if response.get("channel_cycle_values"):
        from mif_pipeline.io.metadata.cycles import resolve_channel_cycles, validate_channel_cycle_values
        submitted = validate_channel_cycle_values(tuple(row.channel_id for row in workflow.biomarker_rows),
            response["channel_cycle_values"])
        if submitted != resolve_channel_cycles(workflow.metadata):
            raise ValueError("Cycle assignments are reviewed in Image inspection. Return there to change the source-plane organization.")
    if content == "raw":
        workflow = apply_stage1_channel_content(workflow, content)
    if response["workflow_status"] in {"needs_biomarkers", "needs_channel_name_replacement"}:
        if not isinstance(biomarker_values, Mapping):
            raise ValueError("stage-one biomarker response is required")
        if type(replacement) is not bool:
            raise ValueError("stage-one biomarker response requires an explicit display choice")
        nuclear = response["nuclear_stain"]
        if nuclear is not None and (type(nuclear) is not str or not nuclear.strip()):
            raise ValueError("Choose an available nuclear stain in Channels and markers")
        components = [dict(row) for row in response["component_values"]]
        biomarker_values = {key: dict(value) for key, value in biomarker_values.items()}
        selected_rows = ([row for row in components if row["component_name"] == nuclear]
            if content == "raw" else [biomarker_values[f"{row.image_index}:{row.channel_index}"]
                for row in workflow.biomarker_rows if row.channel_id == nuclear])
        if nuclear is not None:
            if len(selected_rows) != 1:
                raise ValueError("Choose an available nuclear stain in Channels and markers")
            selected_rows[0].update(assignment="none", biomarker=None)
        from mif_pipeline.io.metadata.canonical import resolve_component_identity_inventory
        from mif_pipeline.preprocessing.inspection.workflow import apply_stage1_nuclear_stain
        if resolve_component_identity_inventory(workflow.metadata, 0) is not None:
            workflow = apply_stage1_nuclear_stain(workflow, None)
        workflow = (apply_stage1_component_values(workflow, components)
                    if content == "raw" else apply_stage1_biomarker_values(workflow, biomarker_values))
        from mif_pipeline.preprocessing.inspection.workflow import apply_stage1_nuclear_stain
        workflow = apply_stage1_nuclear_stain(workflow, response["nuclear_stain"])
        workflow = apply_stage1_channel_name_replacement(
            workflow,
            replace_channel_names_with_biomarkers=replacement,
        )
    else:
        raise ValueError("ready Stage-one workflow cannot consume a response")
    return apply_stage1_channel_content(workflow, content)


def _stage1_review_payload(
    workflow: Stage1Workflow, inspection_ref: BatchArtifactRef, authority: InspectedSourceAuthority
) -> bytes:
    if workflow.status != "ready":
        raise ValueError("only a ready Stage-one workflow can be published")
    unsigned = {
        "schema_name": "mif-stage1-review-artifact",
        "schema_version": 1,
        "source_inspection_artifact_ref_sha256": inspection_ref.artifact_ref_sha256,
        "source_inspection_authority_sha256": authority.content_sha256,
        "reviewed_ome_xml": to_xml(workflow.metadata),
        "replace_channel_names_with_biomarkers": workflow.replace_channel_names_with_biomarkers,
        "checkpoint": {
            "schema_version": 2,
            "source_path": str(workflow.source_path),
            "replace_channel_names_with_biomarkers": workflow.replace_channel_names_with_biomarkers,
            "source_image_index": workflow.selection.source_image_index,
            "source_series_index": workflow.selection.source_series_index,
            "source_image_id": workflow.selection.image_id,
            "source_pixels_id": workflow.selection.pixels_id,
            "source_series_name": workflow.selection.series_name,
        },
    }
    return canonical_bytes({**unsigned, "content_sha256": canonical_digest(unsigned)})


def _load_stage1_review(ref: BatchArtifactRef) -> tuple[object, Mapping[str, object]]:
    if ref.locator is None or ref.sha256 is None or ref.byte_count is None:
        raise ValueError("Stage-one review artifact is not file-backed")
    payload = ref.locator.read_bytes()
    if len(payload) != ref.byte_count or hashlib.sha256(payload).hexdigest() != ref.sha256:
        raise ValueError("Stage-one review artifact bytes changed")
    return _parse_stage1_review(payload)


def _parse_stage1_review(payload: bytes) -> tuple[object, Mapping[str, object]]:
    value = json.loads(payload.decode("utf-8"))
    expected = {
        "schema_name",
        "schema_version",
        "source_inspection_artifact_ref_sha256",
        "source_inspection_authority_sha256",
        "reviewed_ome_xml",
        "replace_channel_names_with_biomarkers",
        "checkpoint",
        "content_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("Stage-one review artifact fields differ")
    unsigned = {key: item for key, item in value.items() if key != "content_sha256"}
    if value["content_sha256"] != canonical_digest(unsigned) or canonical_bytes(value) != payload:
        raise ValueError("Stage-one review artifact digest changed")
    return from_xml(value["reviewed_ome_xml"]), value


def _channel_review_path(sample, *, revision_node_id=None):
    root = artifact_path(sample, "stage1")
    if revision_node_id is not None:
        root = root / "revisions" / revision_node_id
    return root / "stage1-review.json"


def _channel_review_workflow(authority, inspection, inputs, node, sample):
    """Reopen accepted assignments as editable defaults from the same inspection."""
    workflow = stage1_workflow_from_inspection(authority)
    revision = node.get("settled_review_revision")
    if revision is not None:
        previous = _channel_review_path(sample, revision_node_id=(
            revision["predecessor_node_id"] if revision["revision"] > 1 else None))
        if previous.exists():
            metadata, saved = _parse_stage1_review(previous.read_bytes())
            if (saved["source_inspection_artifact_ref_sha256"] != inspection.artifact_ref_sha256
                    or saved["source_inspection_authority_sha256"] != authority.content_sha256):
                raise ValueError("Saved channel assignments belong to a different source inspection.")
            from mif_pipeline.preprocessing.inspection.workflow import _build_workflow
            workflow = _build_workflow(workflow.source_path, metadata, workflow.series, workflow.selection,
                replace_channel_names_with_biomarkers=saved["replace_channel_names_with_biomarkers"])
            return replace(workflow, status="needs_biomarkers"), False
    panel = next((item for item in inputs if item.artifact_kind == "reviewed_panel_mapping"), None)
    if panel is not None:
        loaded = load_reusable_preprocessing_input(panel)
        if type(loaded.record) is not ReviewedPanelMapping:
            raise ValueError("reviewed panel input resolved to the wrong owner type")
        workflow = apply_reviewed_panel_mapping(workflow, loaded.record)
    return ((replace(workflow, status="needs_biomarkers"), False) if revision is not None
            else (workflow, panel is not None))


def execute_channel_review(
    request: PreprocessingJobRequest,
    authorization: Mapping[str, object],
    sample: Mapping[str, object],
    inputs: tuple[BatchArtifactRef, ...],
    response: Mapping[str, object] | None,
    *,
    node: Mapping[str, object],
) -> tuple[StageResult, tuple[BatchArtifactRef, ...], dict[str, object]]:
    inspection = next((item for item in inputs if item.artifact_kind == "source_inspection"), None)
    if inspection is None:
        raise ValueError("channel review requires source inspection")
    authority = _load_authority(inspection)
    workflow, reused_panel = _channel_review_workflow(authority, inspection, inputs, node, sample)
    body = response_body(response)
    if reused_panel:
        if body is not None:
            raise ValueError("reviewed panel mapping cannot be combined with a manual response")
    elif body is not None:
        workflow = _apply_stage1_response(
            workflow,
            body,
            authority=authority,
        )
    if workflow.status != "ready":
        return (
            stage1_workflow_to_stage_result(
                workflow,
                slide_id=require_text(sample.get("slide_id"), "slide_id"),
                inspection_artifact_ref_sha256=inspection.artifact_ref_sha256,
                inspection_authority_sha256=authority.content_sha256,
            ),
            (),
            {"workflow_status": workflow.status, "reused_panel_mapping": False},
        )
    payload = _stage1_review_payload(workflow, inspection, authority)
    locator = _channel_review_path(sample, revision_node_id=(
        request.node_id if node.get("settled_review_revision") is not None else None))
    publish_exact(locator, payload)
    channels = _channel_inventory(workflow.metadata)
    rows = [
        {
            "channel_id": row.channel_id,
            "fluorophore": row.fluorophore,
            "component_name": row.component_name,
            "biomarker": row.biomarker,
            "assignment": row.biomarker_assignment,
        }
        for row in workflow.biomarker_rows
    ]
    component = resolve_component_identity_inventory(workflow.metadata, 0)
    components = [] if component is None else list(component.component_names)
    fluorophores = [row.fluorophore for row in workflow.biomarker_rows]
    biomarkers = (list(component.component_biomarkers)
                  if component is not None and component.component_biomarkers is not None
                  else [row.biomarker for row in workflow.biomarker_rows])
    reviewed_ome_sha = hashlib.sha256(to_xml(workflow.metadata).encode()).hexdigest()
    versions = runtime_versions(ome_types=package_version("ome-types", "unknown"))
    scientific = {
        "components": components,
        "fluorophores": fluorophores,
        "biomarkers": biomarkers,
        "replace_names": workflow.replace_channel_names_with_biomarkers,
    }
    identity = {
        "equivalence": equivalence(
            canonical={"inspection": authority.content_sha256, "ome": reviewed_ome_sha},
            inventory=channels,
            scientific=scientific,
            qc={"workflow_status": workflow.status},
            configuration_sha256=request.resolved_config_sha256,
            runtime_versions_value=versions,
        ),
        "authority_sha256": canonical_digest(json.loads(payload.decode("utf-8"))),
        "source_inspection_authority_sha256": authority.content_sha256,
        "reviewed_ome_sha256": reviewed_ome_sha,
        "channel_inventory_sha256": canonical_digest(channels),
        "component_inventory_sha256": canonical_digest(components),
        "fluorophore_inventory_sha256": canonical_digest(fluorophores),
        "biomarker_inventory_sha256": canonical_digest(biomarkers),
        "replace_channel_names_with_biomarkers": workflow.replace_channel_names_with_biomarkers,
    }
    ref = artifact_ref(
        request=request,
        authorization=authorization,
        sample=sample,
        artifact_kind="stage1_review",
        locator=locator,
        file_sha256_value=hashlib.sha256(payload).hexdigest(),
        byte_count=len(payload),
        owning_identity=identity,
        runtime_versions_value=versions,
        compatibility_material={
            "inspection": inspection.compatibility_signature_sha256,
            "channels": rows,
        },
    )
    return (
        StageResult.completed(stage="channel_review", slide_id=sample["slide_id"]),
        (ref,),
        {
            "workflow_status": "ready",
            "channel_count": len(channels),
            "reused_panel_mapping": reused_panel,
        },
    )


__all__ = [
    "execute_channel_review",
    "execute_inspection",
    "stage1_workflow_to_stage_result",
]
