"""Local annotation editing reuses the exact paused-request exchange used by streaming."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import Path
from copy import deepcopy
from .cluster import cluster_annotations as exchange

# ==============================================================================
#                            Local annotation review
# ==============================================================================


def edit_annotations(
    service,
    runner,
    batch_id,
    request_id,
    expected_head,
    entry_id,
    draft_folder,
    *,
    preview_selection=None,
    reference_role=None,
    image_appearance_path=None,
    cell_removal_node_id=None,
):
    options = {} if preview_selection is None else {"preview_selection": preview_selection}
    if reference_role is not None:
        options["reference_role"] = reference_role
    ticket = (exchange.export_cell_removals(service, batch_id, cell_removal_node_id,
                  expected_head, entry_id) if cell_removal_node_id is not None
              else exchange.export_annotations(service, batch_id, request_id,
                  expected_head, entry_id, **options))
    if cell_removal_node_id is not None and entry_id is None:
        entry_id = ticket["entry_id"]
    scope = (batch_id, request_id, expected_head, entry_id)
    if (
        tuple(ticket.get(k) for k in ("batch_id", "request_id", "state_head_sha256", "entry_id"))
        != scope
    ):
        raise ValueError("The annotation request changed. Reopen the current workflow step.")
    draft = exchange.prepare_local_project(
        runner, Path(draft_folder), Path(ticket["image_path"]), ticket
    )
    completed = runner.run_interactive_project(draft, log_directory=draft.parent / "logs",
                                               image_appearance_path=image_appearance_path)
    if (
        getattr(completed, "returncode", None) != 0
        or getattr(getattr(completed, "drain_evidence", None), "termination_reason", None)
        != "normal_exit"
    ):
        raise ValueError(
            "QuPath did not close normally. Your annotation draft is retained at "
            + str(draft)
            + ". Reopen and check the draft before returning its annotations."
        )
    returned, objects = exchange.collect_local_annotations(runner, draft)
    if (
        returned.get("ticket_id") != ticket["ticket_id"]
        or tuple(
            returned.get(k) for k in ("batch_id", "request_id", "state_head_sha256", "entry_id")
        )
        != scope
    ):
        raise ValueError(
            "The saved annotations belong to another review. Reopen the current annotation step."
        )
    result = exchange.import_annotations(service, batch_id, ticket["ticket_id"], objects)
    return {**result, "local_annotation_draft": str(draft)}


def edit_cell_correction(service, runner, batch_id, *, ticket, draft_folder, image_appearance_path=None, recovery=None):
    """Return the queued existing outlines from one local annotation draft."""
    return edit_cell_corrections(service, runner, batch_id, tickets=[ticket], draft_folder=draft_folder,
        image_appearance_path=image_appearance_path, recovery=recovery)[0]


def edit_cell_corrections(service, runner, batch_id, *, tickets, draft_folder, image_appearance_path=None, image_names=None,
                          recovery=None):
    """Edit an ordered collection in one native session; preview each exact ticket."""
    tickets = list(tickets)
    if not tickets or any(ticket.get("batch_id") != batch_id for ticket in tickets):
        raise ValueError("Select the collected corrections for this analysis.")
    recovery = {} if recovery is None else recovery
    resumed = bool(recovery.get('project'))
    if resumed:
        if recovery['tickets'] != tickets:
            raise ValueError('The saved native draft differs from the queued corrections.')
        draft = Path(recovery['project'])
    else:
        draft = (exchange.prepare_local_project(runner, Path(draft_folder), Path(tickets[0]['image_path']), tickets[0])
                 if len(tickets) == 1 else exchange.prepare_local_correction_project(
                     runner, Path(draft_folder), tickets, image_names=image_names))
        # Publish the exact native mapping before launch: failures must leave a
        # reachable draft even when no candidate has passed validation yet.
        recovery.update(project=str(draft), tickets=deepcopy(tickets))
    recovery['pending'] = True
    try:
        completed = runner.run_interactive_project(draft, log_directory=draft.parent / 'logs',
                                                   image_appearance_path=image_appearance_path)
        if (getattr(completed, 'returncode', None) != 0
                or getattr(getattr(completed, 'drain_evidence', None), 'termination_reason', None) != 'normal_exit'):
            raise ValueError('QuPath did not close normally.')
        returned = ([exchange.collect_local_annotations(runner, draft)] if len(tickets) == 1
                    else exchange.collect_local_corrections(runner, draft))
        if len(returned) != len(tickets) or any(
                {key: value for key, value in saved.items() if key != 'local_image_path'} != ticket
                for (saved, _), ticket in zip(returned, tickets)):
            raise ValueError('The returned outlines belong to another cell correction draft.')
        candidates = []
        for ticket, (_, objects) in zip(tickets, returned):
            if resumed:
                # Preview submissions are immutable. Reopening the native draft
                # may change every image, including an earlier valid candidate.
                scope = ticket['cell_correction']
                ticket = service.prepare_cell_correction(batch_id,
                    artifact_ref_sha256=ticket['segmentation_result_ref_sha256'],
                    expected_head_sha256=ticket['state_head_sha256'], operations=scope['operations'],
                    review_bounds_level0=scope.get('review_bounds_level0'))
            candidates.append({**service.preview_cell_correction(batch_id,
                ticket_id=ticket['ticket_id'], objects=objects), 'local_annotation_draft': str(draft)})
        recovery['pending'] = False
        return candidates
    except Exception as error:
        failure = ValueError(f'{error} Your saved outline edits are retained. '
                             'Choose Edit outlines to check them.')
        failure.add_note(f'Saved correction project: {draft}')
        raise failure from error
