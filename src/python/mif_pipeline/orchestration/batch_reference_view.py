"""Issue visual-only QuPath guidance from current orchestration authorities.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import Path

from .batch_artifacts import BatchArtifactError
from .batch_interactions import (
    BatchInteractionError,
    BatchInteractionTarget,
    ReferenceOnlyQuPathView,
)
from .batch_project import BatchProjectCatalogue
from .batch_registration import decode_committed_registration_result
from .batch_state import BatchExecutionSnapshot

# ==============================================================================
#                              Batch reference view
# ==============================================================================


def is_reference_handoff(target: BatchInteractionTarget) -> bool:
    """Use the owner's existing QuPath-opening field, not a context registry."""
    if not isinstance(target, BatchInteractionTarget):
        return False
    path = target.stage_result.resume_context.get("qupath_project_path")
    return isinstance(path, str) and bool(path.strip())


def issue_reference_only_view(
    snapshot: BatchExecutionSnapshot,
    catalogue: BatchProjectCatalogue,
    target: BatchInteractionTarget,
) -> ReferenceOnlyQuPathView | None:
    """Associate the review with its selected image pair and current saved project.

    We resolve entries through the accepted import index and retain the current
    execution snapshot for opening the view. Native annotation saves advance that
    snapshot while the unchanged image entries remain associated with their index.
    """
    if not is_reference_handoff(target):
        return None
    if not isinstance(snapshot, BatchExecutionSnapshot) or not isinstance(
        catalogue, BatchProjectCatalogue
    ):
        raise BatchInteractionError("reference view requires current typed authorities")
    head = snapshot.head
    if (
        snapshot.plan.plan_sha256 != head.active_plan_sha256
        or target.interaction_request_id not in head.pending_interaction_ids
        or catalogue.batch_id != head.batch_id
        or catalogue.project_identity_sha256 != head.project_identity_sha256
        or catalogue.catalogue_sha256 != head.project_catalogue_sha256
    ):
        raise BatchInteractionError("reference view current head or project authority changed")

    registrations = []
    for authority in snapshot.committed_artifact_authorities:
        if authority.artifact_ref.artifact_kind != "registration_result":
            continue
        try:
            registration = decode_committed_registration_result(authority)
        except BatchArtifactError as exc:
            raise BatchInteractionError("reference view registration authority is invalid") from exc
        if (
            authority.state_head_sha256 != head.sha256
            or authority.active_plan_sha256 != head.active_plan_sha256
        ):
            raise BatchInteractionError("reference view registration authority is stale")
        if (
            registration.sample_id == target.sample_id
            or registration.primary_project_entry_id == target.project_entry_id
        ):
            registrations.append(registration)
    if not registrations:
        # QuPath handoffs may occur before registration has completed.
        return None
    if len(registrations) != 1:
        raise BatchInteractionError("reference view registration scope is ambiguous")
    registration = registrations[0]
    if (
        registration.sample_id != target.sample_id
        or registration.slide_id != target.slide_id
        or registration.validation_domain_id != target.validation_domain_id
        or registration.primary_project_entry_id != target.project_entry_id
        or registration.project_identity_sha256 != head.project_identity_sha256
    ):
        raise BatchInteractionError("reference view registration and target scope differ")
    if registration.mode not in {"side_by_side", "warpy"}:
        return None

    project = Path(head.project_path)
    if (
        not project.is_absolute()
        or project.suffix != ".qpproj"
        or not project.is_file()
        or catalogue.project_uri != project.as_uri()
        or target.stage_result.resume_context["qupath_project_path"] != str(project)
        or head.sample_project_entry_id_by_sample.get(target.sample_id) != target.project_entry_id
    ):
        raise BatchInteractionError("reference view requires the current local QuPath project")
    try:
        primary = catalogue.resolve(registration.primary_project_entry_id)
        moving = catalogue.resolve(registration.moving_project_entry_id)
    except KeyError as exc:
        raise BatchInteractionError("reference view paired entries are missing") from exc
    if (
        primary.image_role != "primary_mif"
        or moving.image_role != "moving_reference"
        or primary.project_entry_id == moving.project_entry_id
        or primary.paired_entry_id != moving.project_entry_id
        or moving.paired_entry_id != primary.project_entry_id
        or primary.image_sha256 != registration.primary_image_sha256
        or moving.image_sha256 != registration.moving_image_sha256
        or moving.reference_modality != registration.moving_reference_modality
        or head.sample_project_entry_projection_sha256_by_sample.get(target.sample_id)
        != primary.entry_projection_sha256
        or any(
            (entry.sample_id, entry.slide_id, entry.validation_domain_id)
            != (target.sample_id, target.slide_id, target.validation_domain_id)
            for entry in (primary, moving)
        )
    ):
        raise BatchInteractionError("reference view reciprocal pair identity or scope changed")
    return ReferenceOnlyQuPathView(
        target_id=target.target_id,
        registration_mode=registration.mode,
        state_head_sha256=head.sha256,
        active_plan_sha256=head.active_plan_sha256,
        project_path=str(project),
        project_identity_sha256=head.project_identity_sha256,
        project_qpproj_snapshot_sha256=head.project_qpproj_snapshot_sha256,
        registration_result_ref_sha256=registration.artifact_ref.artifact_ref_sha256,
        primary_project_entry_id=primary.project_entry_id,
        primary_image_sha256=primary.image_sha256,
        moving_project_entry_id=moving.project_entry_id,
        moving_image_sha256=moving.image_sha256,
        moving_reference_modality=moving.reference_modality,
    )
