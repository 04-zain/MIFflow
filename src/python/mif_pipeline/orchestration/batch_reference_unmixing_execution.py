"""Reference-unmixing lifecycle composition for one batch sample.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path

from ome_types import OME, to_xml, from_xml

from mif_pipeline.io import (
    InspectedSourceAuthority,
    resolve_biomarker_assignment_rows,
    resolve_component_identity_inventory,
)
from mif_pipeline.pipeline_core import (
    InteractionAction,
    InteractionReason,
    StageResult,
)
from mif_pipeline.preprocessing.unmixing.validation import (
    AfBehavior,
    SolverKind,
    SpectralState,
)

from .batch_artifacts import BatchArtifactRef
from .batch_unmixed_copy import ConfirmedUnmixedCopyCompletion, execute_confirmed_unmixed_copy
from .batch_preprocessing_support import (
    REFERENCE_UNMIXING_START_RESPONSE_VERSION,
    artifact_path,
    artifact_ref,
    equivalence,
    publish_exact,
    reference_unmixing_start_response_to_request,
    response_body,
    runtime_versions,
)
from .batch_preprocessing_reuse import (
    LoadedReusablePreprocessingInput,
    load_reusable_preprocessing_input,
)
from .batch_preprocessing_wire import (
    PreprocessingJobRequest,
    canonical_bytes,
    canonical_digest,
    require_sha256,
    require_text,
)
from .batch_stage1_execution import (
    _load_authority,
    _load_stage1_review,
    _channel_review_path,
    _validate_source_selection,
)
from .reference_unmixing import (
    ReferenceUnmixingCompletion,
    ReferenceUnmixingOperation,
    ReferenceUnmixingRequest,
    ReferenceUnmixingState,
    ReferenceUnmixingStatus,
    configure_reference_unmixing_action,
)

REFERENCE_UNMIXING_INTERACTION_VERSION = "mif-batch-reference-unmixing-interaction/v2"
REFERENCE_UNMIXING_ACTION_RESPONSE_VERSION = "mif-batch-reference-unmixing-action/v2"


# ==============================================================================
#                       Batch reference unmixing execution
# ==============================================================================


def _load_reviewed_source_pair(
    inspection: BatchArtifactRef,
    review: BatchArtifactRef,
    sample: Mapping[str, object],
) -> tuple[InspectedSourceAuthority, OME, bool]:
    """Authenticate the exact Stage-1 review against its inspected source."""
    authority = _load_authority(inspection)
    _validate_source_selection(authority, sample)
    source_identity = authority.source_identity
    if (
        inspection.owning_identity.get("source")
        != {
            "file_sha256": source_identity.file_sha256,
            "size_bytes": source_identity.size_bytes,
            "modified_time_ns": source_identity.modified_time_ns,
            "device_id": source_identity.device_id,
            "inode_id": source_identity.inode_id,
        }
        or inspection.owning_identity.get("ome_sha256")
        != hashlib.sha256(authority.canonical_ome_xml.encode()).hexdigest()
    ):
        raise ValueError("source inspection owning identity differs from inspected authority")
    reviewed_ome, record = _load_stage1_review(review)
    if not isinstance(reviewed_ome, OME) or len(reviewed_ome.images) != 1:
        raise ValueError("Stage-one review requires one selected OME image")
    identity = review.owning_identity
    channels = [
        {"id": channel.id, "name": channel.name, "fluor": channel.fluor}
        for channel in reviewed_ome.images[0].pixels.channels
    ]
    rows = resolve_biomarker_assignment_rows(reviewed_ome)
    component = resolve_component_identity_inventory(reviewed_ome, 0)
    components = [] if component is None else list(component.component_names)
    if (
        record.get("schema_name") != "mif-stage1-review-artifact"
        or type(record.get("schema_version")) is not int
        or record["schema_version"] != 1
        or record["source_inspection_artifact_ref_sha256"] != inspection.artifact_ref_sha256
        or record["source_inspection_authority_sha256"] != authority.content_sha256
        or identity.get("authority_sha256") != canonical_digest(record)
        or identity.get("source_inspection_authority_sha256") != authority.content_sha256
        or identity.get("reviewed_ome_sha256")
        != hashlib.sha256(to_xml(reviewed_ome).encode()).hexdigest()
        or identity.get("channel_inventory_sha256") != canonical_digest(channels)
        or identity.get("component_inventory_sha256") != canonical_digest(components)
        or identity.get("fluorophore_inventory_sha256")
        != canonical_digest([row.fluorophore for row in rows])
        or identity.get("biomarker_inventory_sha256")
        != canonical_digest(list(component.component_biomarkers)
            if component is not None and component.component_biomarkers is not None
            else [row.biomarker for row in rows])
    ):
        raise ValueError("Stage-one review is foreign to source inspection")
    replacement = record["replace_channel_names_with_biomarkers"]
    checkpoint = record["checkpoint"]
    selection = authority.selection
    if (
        type(replacement) is not bool
        or identity.get("replace_channel_names_with_biomarkers") is not replacement
        or not isinstance(checkpoint, Mapping)
        or checkpoint.get("schema_version") != 2
        or checkpoint.get("source_path") != str(authority.source_identity.resolved_path)
        or checkpoint.get("replace_channel_names_with_biomarkers") is not replacement
        or checkpoint.get("source_image_index") != selection.source_image_index
        or checkpoint.get("source_series_index") != selection.source_series_index
        or checkpoint.get("source_image_id") != selection.image_id
        or checkpoint.get("source_pixels_id") != selection.pixels_id
        or checkpoint.get("source_series_name") != selection.series_name
    ):
        raise ValueError("Stage-one review checkpoint is foreign to inspected selection")
    return authority, reviewed_ome, replacement


def _reference_paths(
    sample: Mapping[str, object],
    inspection: BatchArtifactRef,
    review: BatchArtifactRef,
    *, revision_node_id: str | None = None,
) -> tuple[str, Path, Path, Path]:
    if revision_node_id is None and review.locator != _channel_review_path(sample):
        # A revised channel review changes the prepared image even when the
        # unmixing settings themselves have not been independently revised.
        revision_node_id = review.producer_node_id
    root = artifact_path(sample, "reference-unmixing")
    if revision_node_id is not None:
        import re
        if re.fullmatch(r"node-[0-9a-f]{48}", revision_node_id) is None:
            raise ValueError("Unmixing revision requires its exact task identity")
        root = root / "revisions" / revision_node_id
    request_path = root / "reference-unmixing-request.json"
    output_path = Path(
        require_text(sample.get("analysis_ready_output_path"), "analysis_ready_output_path")
    )
    if revision_node_id is not None:
        output_path = root / "analysis-ready.ome.zarr"
    if not output_path.is_absolute():
        raise ValueError("analysis-ready output path must be absolute")
    sample_root = Path(
        require_text(sample.get("sample_output_root"), "sample_output_root")
    ).resolve()
    try:
        output_path.resolve().relative_to(sample_root)
    except ValueError as exc:
        raise ValueError("analysis-ready output escapes sample root") from exc
    run_id = (
        "run-"
        + canonical_digest(
            {
                "sample_id": sample["sample_id"],
                "source_inspection_artifact_ref_sha256": inspection.artifact_ref_sha256,
                "source_inspection_compatibility_signature_sha256": inspection.compatibility_signature_sha256,
                "stage1_review_artifact_ref_sha256": review.artifact_ref_sha256,
                "stage1_review_compatibility_signature_sha256": review.compatibility_signature_sha256,
                "resolved_config_sha256": sample["resolved_config_sha256"],
                **({"revision_node_id": revision_node_id} if revision_node_id else {}),
            }
        )[:48]
    )
    return run_id, root, request_path, output_path


def _reference_revision_node_id(node):
    return node["node_id"] if node.get("settled_review_revision") is not None else None


def _previous_reference_request(node, sample, inspection, review, authority, reviewed_ome, replacement):
    """Read defaults only from the exact preceding revision of this same source."""
    raw = node.get("settled_review_revision")
    if raw is None:
        return None, None
    from .batch_settled_review_revision import SettledReviewRevision

    revision = SettledReviewRevision.from_mapping(raw)
    run_id, root, path, output = _reference_paths(sample, inspection, review,
        revision_node_id=revision.predecessor_node_id if revision.revision > 1 else None)
    if not path.exists():
        return None, None  # The previous review stopped before any settings were saved.
    saved, _ = _load_reference_request(path)
    _require_current_source_request(saved, run_id=run_id, authority=authority,
        reviewed_ome=reviewed_ome, replacement=replacement, output_path=output)
    return saved, root


def saved_unmixing_settings(snapshot, node):
    """Project the exact saved request for passive editing; never start an operation.

    The current snapshot supplies the selected inspection and channel review.
    Reuse the normal start-form projection and request/source compatibility check;
    the resulting form is display data, not a pending interaction authority.
    """
    from .batch_preprocessing_support import _resolved_sample_mapping

    selected = next(item for item in snapshot.manifest.enabled_samples
                    if (item.sample_id,) == node.participant_ids)
    sample = _resolved_sample_mapping(selected, "reference_unmixing")
    refs = {ref.artifact_id: ref for ref in snapshot.accepted_artifacts}
    inputs = [refs[key] for key in node.input_artifact_ids if key in refs]
    inspection = next(ref for ref in inputs if ref.artifact_kind == "source_inspection")
    review = next(ref for ref in inputs if ref.artifact_kind == "stage1_review")
    authority, reviewed_ome, replacement = _load_reviewed_source_pair(inspection, review, sample)
    run_id, root, path, output = _reference_paths(sample, inspection, review,
        revision_node_id=_reference_revision_node_id(node.to_mapping()))
    if not path.exists():
        from mif_pipeline.pipeline_core import StageStatus
        if snapshot.head.node_status_by_id.get(node.node_id) is StageStatus.COMPLETED:
            raise ValueError("The saved unmixing settings are unavailable. Restore the saved request before editing this completed step.")
        return None  # A stopped start has no saved settings to display.
    request, _ = _load_reference_request(path)
    _require_current_source_request(request, run_id=run_id, authority=authority,
        reviewed_ome=reviewed_ome, replacement=replacement, output_path=output)
    return _reference_start_stage_result(slide_id=selected.slide_id,
        inspection=inspection, review=review, operation_root=root, request_path=path,
        candidate_library=next((ref for ref in inputs if ref.artifact_kind == "spectral_reference_library"), None),
        reviewed_ome=reviewed_ome, previous_request=request)


def _reuse_previous_reference_library(previous_request, previous_root, current_request, operation_root):
    """Offer a retained accepted library through ordinary import and its compatibility review."""
    if previous_request is None or previous_root is None:
        return
    from mif_pipeline.io.metadata.canonical import resolve_unmixing_input_metadata

    before = resolve_unmixing_input_metadata(from_xml(previous_request.reviewed_ome_xml), 0)
    after = resolve_unmixing_input_metadata(from_xml(current_request.reviewed_ome_xml), 0)
    if before is None or before != after:
        return
    if not (previous_root / ".reference-unmixing-operation").exists():
        return  # A failed start or already-unmixed copy has no reference library.
    try:
        ReferenceUnmixingOperation.open_existing(previous_root).export_library(
            previous_request.run_id, operation_root / "previous-reference-library.miflib.zip")
    except ValueError as exc:
        if str(exc) != "No accepted portable library is available to export yet":
            raise


def _reference_start_stage_result(
    *,
    slide_id: str,
    inspection: BatchArtifactRef,
    review: BatchArtifactRef,
    operation_root: Path,
    request_path: Path,
    candidate_library: BatchArtifactRef | None,
    reviewed_ome: OME | None = None,
    previous_request: ReferenceUnmixingRequest | None = None,
) -> StageResult:
    fields = [
        {"name": "solver", "kind": "enum", "choices": [item.value for item in SolverKind]},
        {
            "name": "spectral_state_override",
            "kind": "nullable_enum",
            "choices": [item.value for item in SpectralState if item is not SpectralState.UNKNOWN],
        },
        {"name": "af_behavior", "kind": "enum", "choices": [item.value for item in AfBehavior]},
        {"name": "chunk_size", "kind": "positive_integer_pair", "choices": []},
        {"name": "calculation_precision", "kind": "enum", "choices": ["float32", "float64"]},
        {"name": "output_dtype", "kind": "enum", "choices": ["float32", "float64"]},
        {"name": "nnls_tolerance", "kind": "positive_number", "choices": []},
        {"name": "nnls_max_iterations", "kind": "positive_integer", "choices": []},
        {"name": "overwrite", "kind": "boolean", "choices": []},
    ]
    fields.sort(key=lambda field: field["name"] != "spectral_state_override")
    service_context = {
        "schema_version": REFERENCE_UNMIXING_INTERACTION_VERSION,
        "interaction_kind": "configure_reference_unmixing",
        "source_inspection_artifact_ref_sha256": inspection.artifact_ref_sha256,
        "source_inspection_compatibility_signature_sha256": inspection.compatibility_signature_sha256,
        "stage1_review_artifact_ref_sha256": review.artifact_ref_sha256,
        "stage1_review_compatibility_signature_sha256": review.compatibility_signature_sha256,
        "operation_root_locator": str(operation_root),
        "operation_status": "unstarted",
        "request_locator": str(request_path),
        "request_present": False,
        "request_byte_count": None,
        "request_sha256": None,
        "response_schema_version": REFERENCE_UNMIXING_START_RESPONSE_VERSION,
        "configuration_fields": fields,
        "candidate_spectral_library_artifact_id": (
            None if candidate_library is None else candidate_library.artifact_id
        ),
        "candidate_spectral_library_artifact_ref_sha256": (
            None if candidate_library is None else candidate_library.artifact_ref_sha256
        ),
        "candidate_spectral_library_compatibility_signature_sha256": (
            None if candidate_library is None else candidate_library.compatibility_signature_sha256
        ),
    }
    from .batch_preprocessing_support import unmixing_source_state, unmixing_af_behavior

    state, source = unmixing_source_state(reviewed_ome)
    service_context.update(
        source_spectral_state=state, spectral_state_source=source,
        source_af_behavior=unmixing_af_behavior(reviewed_ome),
    )
    if reviewed_ome is not None:
        from mif_pipeline.io.metadata.measurement_band_copy import measurement_band_mapping_context
        service_context["measurement_band_mapping"] = measurement_band_mapping_context(reviewed_ome)
    if reviewed_ome is not None and state != "unmixed":
        from mif_pipeline.io.metadata.calibration import calibration_inventory

        inventory = calibration_inventory(reviewed_ome)
        if inventory.get("correction_mode") == "as_stored":
            service_context["calibration_request"] = inventory
        if inventory["missing"]:
            from .batch_preprocessing_support import (
                REFERENCE_UNMIXING_CALIBRATION_RESPONSE_VERSION,
            )

            service_context["calibration_request"] = inventory
            service_context["response_schema_version"] = (
                REFERENCE_UNMIXING_CALIBRATION_RESPONSE_VERSION
            )
    if previous_request is not None:
        from mif_pipeline.io.metadata.calibration import calibration_review
        defaults = {field["name"]: getattr(previous_request, field["name"]) for field in fields
                    if getattr(previous_request, field["name"]) is not None}
        defaults["chunk_size"] = list(defaults["chunk_size"])
        # A fresh output path never needs permission to overwrite earlier results.
        defaults["overwrite"] = False
        if previous_request.measurement_band_components:
            defaults["measurement_band_components"] = list(previous_request.measurement_band_components)
        proof = calibration_review(from_xml(previous_request.reviewed_ome_xml))
        if proof is not None:
            defaults["calibration_correction_mode"] = proof["correction_mode"]
            defaults["calibration_source"] = proof.get("source_note", "")
            for i, row in enumerate(service_context.get("calibration_request", {}).get("missing", ())):
                if row["key"] in proof["values"]:
                    defaults[f"calibration_{i}"] = proof["values"][row["key"]]
        service_context["initial_configuration"] = defaults
    return StageResult.needs_interaction(
        stage="reference_unmixing",
        slide_id=slide_id,
        reason=InteractionReason.OTHER_EXPLICIT_REASON,
        message="Confirm the image spectral state, then select its unmixing or component-image output settings.",
        allowed_actions=(InteractionAction.ACCEPT.value,),
        resume_context={
            "schema_version": REFERENCE_UNMIXING_INTERACTION_VERSION,
            "service_context": service_context,
        },
        checkpoint_id=canonical_digest(service_context),
    )


def reference_unmixing_state_to_stage_result(
    state: ReferenceUnmixingState,
    *,
    slide_id: str,
    inspection_artifact_ref_sha256: str,
    inspection_compatibility_signature_sha256: str,
    review_artifact_ref_sha256: str,
    review_compatibility_signature_sha256: str,
    operation_root: Path,
    request_path: Path,
    request_sha256: str,
    request_byte_count: int,
    candidate_library: BatchArtifactRef | None = None,
) -> StageResult:
    """Translate one bounded owner state without copying scientific payloads."""

    if not isinstance(state, ReferenceUnmixingState):
        raise TypeError("reference StageResult conversion requires owner state")
    if state.status is ReferenceUnmixingStatus.ABORTED:
        return StageResult.failed(
            stage="reference_unmixing",
            slide_id=slide_id,
            error_code="reference_unmixing_aborted",
            error_message="Reference/unmixing workflow was explicitly aborted.",
            error_context={"run_id": state.run_id, "checkpoint_token": state.checkpoint_token},
        )
    service_context = {
        "schema_version": REFERENCE_UNMIXING_INTERACTION_VERSION,
        "interaction_kind": "reference_unmixing_operation",
        "source_inspection_artifact_ref_sha256": require_sha256(
            inspection_artifact_ref_sha256, "inspection artifact ref"
        ),
        "source_inspection_compatibility_signature_sha256": require_sha256(
            inspection_compatibility_signature_sha256, "inspection compatibility"
        ),
        "stage1_review_artifact_ref_sha256": require_sha256(
            review_artifact_ref_sha256, "Stage-one review artifact ref"
        ),
        "stage1_review_compatibility_signature_sha256": require_sha256(
            review_compatibility_signature_sha256, "Stage-one review compatibility"
        ),
        "operation_root_locator": str(operation_root),
        "operation_status": state.status.value,
        "request_locator": str(request_path),
        "request_present": True,
        "request_byte_count": request_byte_count,
        "request_sha256": require_sha256(request_sha256, "request SHA-256"),
        "response_schema_version": REFERENCE_UNMIXING_ACTION_RESPONSE_VERSION,
        "operation_state": state.to_mapping(),
        "candidate_spectral_library_artifact_id": (
            None if candidate_library is None else candidate_library.artifact_id
        ),
        "candidate_spectral_library_artifact_ref_sha256": (
            None if candidate_library is None else candidate_library.artifact_ref_sha256
        ),
        "candidate_spectral_library_compatibility_signature_sha256": (
            None if candidate_library is None else candidate_library.compatibility_signature_sha256
        ),
    }
    reusable = operation_root / "previous-reference-library.miflib.zip"
    if state.checkpoint == "basis_source" and reusable.is_file():
        service_context["revision_library_locator"] = str(reusable)
    return StageResult.needs_interaction(
        stage="reference_unmixing",
        slide_id=slide_id,
        reason=InteractionReason.OTHER_EXPLICIT_REASON,
        message="Complete the current reference-library, target-AF, or final-basis review.",
        allowed_actions=(InteractionAction.ACCEPT.value,),
        resume_context={
            "schema_version": REFERENCE_UNMIXING_INTERACTION_VERSION,
            "service_context": service_context,
        },
        checkpoint_id=state.checkpoint_token,
        generation_id=state.run_id,
    )


def _load_reference_request(path: Path) -> tuple[ReferenceUnmixingRequest, bytes]:
    payload = path.read_bytes()
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict) or canonical_bytes(value) != payload:
        raise ValueError("reference-unmixing request bytes are not canonical")
    return ReferenceUnmixingRequest.from_mapping(value), payload


def _require_current_source_request(
    owner_request: ReferenceUnmixingRequest,
    *,
    run_id: str,
    authority: InspectedSourceAuthority,
    reviewed_ome: OME,
    replacement: bool,
    output_path: Path,
) -> None:
    from mif_pipeline.io.metadata.calibration import replay_calibration

    reviewed_ome = replay_calibration(reviewed_ome, from_xml(owner_request.reviewed_ome_xml))
    # The loaded request already validated its numerical settings and source
    # binding. A comparison request would copy those same settings and parse the
    # same metadata again. Compare only the independently supplied source facts.
    if type(authority) is not InspectedSourceAuthority or not isinstance(reviewed_ome, OME):
        raise TypeError("reviewed source requires exact inspection and OME authority")
    if (
        type(run_id) is not str
        or owner_request.run_id != run_id
        or owner_request.source_kind != "reviewed_source"
        or owner_request.input_path != str(authority.source_identity.resolved_path)
        or owner_request.output_path != str(output_path)
        or owner_request.inspected_authority_json != authority.to_json_bytes().decode("utf-8")
        or owner_request.reviewed_ome_xml != to_xml(reviewed_ome)
        or owner_request.replace_channel_names_with_biomarkers is not replacement
    ):
        raise ValueError("persisted reference-unmixing request is foreign")


def _completion_artifact(
    *,
    request: PreprocessingJobRequest,
    authorization: Mapping[str, object],
    sample: Mapping[str, object],
    inspection: BatchArtifactRef,
    review: BatchArtifactRef,
    authority: InspectedSourceAuthority,
    completion: ReferenceUnmixingCompletion | ConfirmedUnmixedCopyCompletion,
) -> BatchArtifactRef:
    if completion.target_image_sha256 != authority.source_identity.file_sha256:
        raise ValueError("reference completion target differs from inspected source")
    output_path = Path(completion.output_path)
    provenance_sha = completion.provenance_sha256
    clipping_sha = completion.clipping_evidence_sha256
    normalization = {
        "final_authority_id": completion.final_authority_id,
        "publication_request_sha256": completion.publication_request_sha256,
    }
    scientific = {
        "config_sha256": completion.config_sha256,
        "final_authority_sha256": completion.final_authority_sha256,
        "normalization": normalization,
        "output_sha256": completion.output_sha256,
    }
    qc = {
        "checkpoint_sha256": completion.checkpoint_sha256,
        "clipping_evidence_sha256": clipping_sha,
        "warnings": list(completion.warnings),
        "publication_warnings": list(completion.publication_warnings),
    }
    versions = runtime_versions(
        unmixing=(
            "already-unmixed-copy/v1"
            if type(completion) is ConfirmedUnmixedCopyCompletion
            else "reference-unmixing-operation/v2"
        )
    )
    identity = {
        "equivalence": equivalence(
            canonical={
                "source_inspection": inspection.artifact_ref_sha256,
                "stage1_review": review.artifact_ref_sha256,
                "target": completion.target_image_sha256,
                "output": completion.output_sha256,
            },
            inventory={
                "regions_processed": completion.regions_processed,
                "pyramid_levels": completion.pyramid_levels,
            },
            scientific=scientific,
            qc=qc,
            configuration_sha256=request.resolved_config_sha256,
            runtime_versions_value=versions,
        ),
        "authority_sha256": completion.content_sha256,
        "source_inspection_artifact_ref_sha256": inspection.artifact_ref_sha256,
        "stage1_review_artifact_ref_sha256": review.artifact_ref_sha256,
        "target_image_sha256": completion.target_image_sha256,
        "final_authority_id": completion.final_authority_id,
        "final_authority_sha256": completion.final_authority_sha256,
        "final_basis_sha256": (
            None
            if type(completion) is ConfirmedUnmixedCopyCompletion
            else completion.final_authority_sha256
        ),
        "normalization_sha256": canonical_digest(normalization),
        "image_generation_sha256": completion.output_sha256,
        "publication": {
            "output_sha256": completion.output_sha256,
            "provenance_sha256": provenance_sha,
            "clipping_evidence_sha256": clipping_sha,
        },
        "operation_completion_sha256": completion.content_sha256,
    }
    return artifact_ref(
        request=request,
        authorization=authorization,
        sample=sample,
        artifact_kind="processed_image",
        locator=output_path,
        file_sha256_value=completion.output_sha256,
        byte_count=(output_path / "zarr.json").stat().st_size,
        owning_identity=identity,
        runtime_versions_value=versions,
        compatibility_material={
            "source_inspection": inspection.compatibility_signature_sha256,
            "stage1_review": review.compatibility_signature_sha256,
            "final_authority": completion.final_authority_sha256,
            "config": completion.config_sha256,
        },
    )


def execute_reference_unmixing(
    request: PreprocessingJobRequest,
    authorization: Mapping[str, object],
    sample: Mapping[str, object],
    inputs: tuple[BatchArtifactRef, ...],
    response: Mapping[str, object] | None,
) -> tuple[StageResult, tuple[BatchArtifactRef, ...], dict[str, object]]:
    inspections = tuple(item for item in inputs if item.artifact_kind == "source_inspection")
    reviews = tuple(item for item in inputs if item.artifact_kind == "stage1_review")
    libraries = tuple(item for item in inputs if item.artifact_kind == "spectral_reference_library")
    if (
        len(inspections) != 1
        or len(reviews) != 1
        or len(libraries) > 1
        or len(inputs) != 2 + len(libraries)
    ):
        raise ValueError("reference unmixing requires one inspection and its Stage-one review")
    inspection, review = inspections[0], reviews[0]
    authority, reviewed_ome, replacement = _load_reviewed_source_pair(
        inspection,
        review,
        sample,
    )
    library_ref = libraries[0] if libraries else None
    loaded_library: LoadedReusablePreprocessingInput | None = None
    from .batch_preprocessing_support import unmixing_source_state

    recorded_state, _recorded_source = unmixing_source_state(reviewed_ome)
    node = json.loads(request.node_json)
    run_id, operation_root, request_path, output_path = _reference_paths(sample, inspection, review,
        revision_node_id=_reference_revision_node_id(node))
    body = response_body(response)
    starting = not request_path.exists()
    if starting:
        previous_request, previous_root = _previous_reference_request(
            node, sample, inspection, review, authority, reviewed_ome, replacement)
        if body is None:
            return (
                _reference_start_stage_result(
                    slide_id=sample["slide_id"],
                    inspection=inspection,
                    review=review,
                    operation_root=operation_root,
                    request_path=request_path,
                    candidate_library=library_ref,
                    reviewed_ome=reviewed_ome,
                    previous_request=previous_request,
                ),
                (),
                {"operation_status": "unstarted"},
            )
        owner_request = reference_unmixing_start_response_to_request(
            body,
            run_id=run_id,
            authority=authority,
            reviewed_ome=reviewed_ome,
            output_path=output_path,
            accepted_name_choice=replacement,
        )
        if owner_request.replace_channel_names_with_biomarkers is not replacement:
            raise ValueError(
                "reference configuration conflicts with accepted Stage-one name choice"
            )
        request_payload = canonical_bytes(owner_request.to_mapping())
        publish_exact(request_path, request_payload)
        if recorded_state == "unmixed" or owner_request.spectral_state_override in {"unmixed", "isolated"}:
            owner_result = execute_confirmed_unmixed_copy(owner_request, operation_root)
        else:
            operation = ReferenceUnmixingOperation(operation_root)
            owner_result = operation.start_from_reviewed_target(owner_request)

    else:
        owner_request, request_payload = _load_reference_request(request_path)
        _require_current_source_request(
            owner_request,
            run_id=run_id,
            authority=authority,
            reviewed_ome=reviewed_ome,
            replacement=replacement,
            output_path=output_path,
        )
        if recorded_state == "unmixed" or owner_request.spectral_state_override in {"unmixed", "isolated"}:
            if body is not None:
                repeated = reference_unmixing_start_response_to_request(
                    body,
                    run_id=run_id,
                    authority=authority,
                    reviewed_ome=reviewed_ome,
                    output_path=output_path,
                    accepted_name_choice=replacement,
                )
                if repeated != owner_request:
                    raise ValueError("The confirmed image-copy request has changed")
            owner_result = execute_confirmed_unmixed_copy(owner_request, operation_root)
        else:
            operation = ReferenceUnmixingOperation.open_existing(operation_root)
            if body is None:
                owner_result = operation.resume(owner_request)
            else:
                if (
                    set(body)
                    != {
                        "schema_version",
                        "checkpoint_token",
                        "option_value",
                        "form_values",
                        "injected_configuration",
                    }
                    or body.get("schema_version") != REFERENCE_UNMIXING_ACTION_RESPONSE_VERSION
                ):
                    raise ValueError("reference-unmixing action response fields differ")
                current = operation.status(run_id)
                if not isinstance(current, ReferenceUnmixingState):
                    raise ValueError("completed reference-unmixing run has unused response")
                compound_regions = body['option_value'] == 'accept_reference_regions'
                from .reference_source_preparation import RESPONSE_ACTIONS, configured_source_response
                source_preparation = body['option_value'] in RESPONSE_ACTIONS
                if body["checkpoint_token"] != current.checkpoint_token and not (compound_regions or source_preparation):
                    raise ValueError("reference-unmixing action response is stale")
                form_values = body["form_values"]
                if not isinstance(form_values, Mapping):
                    raise ValueError("reference-unmixing action form values are invalid")
                resolved_form_values = dict(form_values)
                if body["option_value"] in {"import_reference_library", "curated_reference_library"}:
                    if library_ref is not None:
                        loaded_library = load_reusable_preprocessing_input(library_ref)
                    if loaded_library is not None:
                        supplied = resolved_form_values.get("library_locator")
                        pinned_library = artifact_path(
                            sample,
                            "reference-unmixing/reusable/" f"{loaded_library.ref.sha256}.json",
                        )
                        if supplied is not None and supplied != str(pinned_library):
                            raise ValueError("manifest spectral library locator is adapter-owned")
                        publish_exact(pinned_library, loaded_library.payload)
                        resolved_form_values["library_locator"] = str(pinned_library)
                if compound_regions:
                    # The accepted response retains every role choice. A prior
                    # attempt may have committed a strict prefix of its phases;
                    # the compound operation verifies that saved lineage itself.
                    from .reference_region_selection import configured_region_response
                    action = configured_region_response(body)
                elif source_preparation:
                    action = configured_source_response(body)
                else:
                    action = configure_reference_unmixing_action(
                        current,
                        option_value=body["option_value"],
                        form_values=resolved_form_values,
                        injected_configuration=body["injected_configuration"],
                    )
                owner_result = operation.advance(owner_request, action)
    if (type(owner_result) is ReferenceUnmixingState and owner_result.checkpoint == "basis_source"
            and node.get("settled_review_revision") is not None and library_ref is None
            and not (operation_root / "previous-reference-library.miflib.zip").exists()):
        if not starting:
            previous_request, previous_root = _previous_reference_request(
                node, sample, inspection, review, authority, reviewed_ome, replacement)
        _reuse_previous_reference_library(previous_request, previous_root, owner_request, operation_root)
    if (
        type(owner_result) is ReferenceUnmixingState
        and owner_result.checkpoint == "final_run_basis"
        and any(
            r.operation_action == "propose_final_basis" for r in owner_result.operation_requirements
        )
    ):
        owner_result = operation.prepare_final_review(
            owner_result.run_id, owner_result.checkpoint_token
        )
    request_sha = hashlib.sha256(request_payload).hexdigest()
    if isinstance(owner_result, (ReferenceUnmixingCompletion, ConfirmedUnmixedCopyCompletion)):
        ref = _completion_artifact(
            request=request,
            authorization=authorization,
            sample=sample,
            inspection=inspection,
            review=review,
            authority=authority,
            completion=owner_result,
        )
        return (
            StageResult.completed(
                stage="reference_unmixing",
                slide_id=sample["slide_id"],
                generation_id=owner_result.run_id,
            ),
            (ref,),
            {
                "operation_status": "completed",
                "regions_processed": owner_result.regions_processed,
                "pyramid_levels": owner_result.pyramid_levels,
            },
        )
    return (
        reference_unmixing_state_to_stage_result(
            owner_result,
            slide_id=sample["slide_id"],
            inspection_artifact_ref_sha256=inspection.artifact_ref_sha256,
            inspection_compatibility_signature_sha256=inspection.compatibility_signature_sha256,
            review_artifact_ref_sha256=review.artifact_ref_sha256,
            review_compatibility_signature_sha256=review.compatibility_signature_sha256,
            operation_root=operation_root,
            request_path=request_path,
            request_sha256=request_sha,
            request_byte_count=len(request_payload),
            candidate_library=library_ref,
        ),
        (),
        {"operation_status": owner_result.status.value},
    )


__all__ = [
    "execute_reference_unmixing",
    "reference_unmixing_state_to_stage_result",
]
