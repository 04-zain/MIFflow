"""

Resolves the current image pair and accepted tissue for an alignment request.

We obtain the selected review from the analysis's pending interactions,
confirm that it contains one admitted image-pair choice, and recover
the accepted mIF tissue regions for that sample. Both automatic fitting and
landmark export use these same image and tissue selections.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname
from .batch_interactions import (
    BatchInteractionError,
    group_interactions,
    project_batch_interaction_target,
)
from mif_pipeline.qupath_runtime.registration_session import CurrentRegistrationProjectSnapshot

# ==============================================================================
#                               Alignment context
# ==============================================================================


def current_alignment_context(store, request):
    """

    Finds the exact pending alignment review selected by the current form.

    We check its action, target and expected analysis state, then match it to
    the saved pending-review group. The selected step supplies the project,
    fixed and moving entries, physical calibration and intended transform path.
    Finally, we collect the tissue annotation IDs from the accepted tissue
    result belonging to the same sample.

    Args:
        store: Current analysis-state store providing pending requests and
            accepted results.
        request: One-target import_warpy_transform response with its expected
            analysis state, review-group ID and group checksum.

    Returns:
        Current state, registration snapshot, project path, transform destination,
        selected tissue annotation IDs and the matched review target.

    Raises:
        BatchInteractionError: The selected action, review group or target no
            longer matches the current alignment step.
        ValueError: A saved transform has no admitted review return, the project
            differs or accepted tissue regions are unavailable.
    """

    if (
        request.action != "import_warpy_transform"
        or len(request.selection.target_ids) != 1
        or dict(request.response) != {}
    ):
        raise ValueError("Automatic alignment requires the current single-image alignment form")
    head = store.load_head()
    if head.sha256 != request.expected_state_head_sha256:
        raise BatchInteractionError("The alignment form has changed; reopen it")
    targets = []
    for key in head.pending_interaction_ids:
        publication = store.reopen_interaction_request_publication(
            key, expected_head_sha256=head.sha256
        )
        targets.append(project_batch_interaction_target(publication))
    matching = [
        g
        for g in group_interactions(tuple(targets))
        if g.group_id == request.group_id and g.group_sha256 == request.group_sha256
    ]
    if len(matching) != 1:
        raise BatchInteractionError("The alignment request no longer matches its review group")
    selected = [t for t in matching[0].targets if t.target_id == request.selection.target_ids[0]]
    if len(selected) != 1:
        raise BatchInteractionError("Select the current alignment image")
    target = selected[0]
    context = target.stage_result.resume_context
    context = context.get("service_context", context)
    if (
        target.stage_result.stage != "registration"
        or context.get("schema_version") != "warpy-registration-interaction/v1"
    ):
        raise BatchInteractionError("The selected review is not an alignment setup")
    snapshot = CurrentRegistrationProjectSnapshot.from_mapping(
        _plain(context["batch_registration_snapshot"])
    )
    execution = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
    node = next(item for item in execution.plan.nodes if item.node_id == target.node_id)
    if snapshot.transform.presence != "absent" and node.settled_review_revision is None:
        raise ValueError("Return to alignment setup before replacing its saved transformation")
    project = Path(url2pathname(urlsplit(snapshot.project_uri).path))
    destination = Path(url2pathname(urlsplit(snapshot.transform.transform_uri).path))
    if project != Path(head.project_path):
        raise ValueError("The alignment project does not match this analysis")
    tissue_ids = []
    for authority in execution.committed_artifact_authorities:
        ref = authority.artifact_ref
        if (
            ref.artifact_kind == "tissue_acceptance_result"
            and ref.scope.sample_id == target.sample_id
        ):
            from .batch_tissue_artifacts import decode_committed_tissue_acceptance_result

            projection = decode_committed_tissue_acceptance_result(authority)
            tissue_ids = [row["path_object_id"] for row in projection.section_inventory]
    if not tissue_ids:
        raise ValueError("The accepted mIF tissue regions are unavailable")
    return head, snapshot, project, destination, tissue_ids, target


def _plain(value):
    """

    Converts nested saved values into the dictionaries and lists required by the image-pair reader.

    Args:
        value: Mapping, tuple, list or scalar from the pending review description.

    Returns:
        Equivalent nested dictionary/list structure with scalar values retained.
    """

    if hasattr(value, "items"):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return value
