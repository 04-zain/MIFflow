"""Reconstruct current saved image hierarchies from accepted project changes."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mif_pipeline.qupath_runtime.catalogue import (
        ProjectAuthorityHead,
        ProjectMutationAttestation,
    )
    from mif_pipeline.qupath_runtime.project_stage import ProjectStageMutationEvidenceRecord

    from .batch_project import BatchProjectCatalogue


def _project_location_history(current, *, load_head):
    """Read accepted project locations without changing their saved records.

    An imported project retains its original identity and mutation history. A
    relocation changes only the active URI in an ordinary successor head. Keep
    those exact transitions for annotation paths and catalogue reconstruction;
    return the heads too so the subsequent saved-state calculation reuses them.
    """
    from .batch_state import BatchStateError
    from mif_pipeline.qupath_runtime.catalogue import _project_path_from_uri

    heads, locations, relocations = {}, {}, []
    cursor = current
    while True:
        if (cursor.authority_head_sha256 in heads
                or cursor.project_identity_sha256 != current.project_identity_sha256):
            raise BatchStateError("project location history is cyclic or belongs to another project")
        heads[cursor.authority_head_sha256] = cursor
        path = _project_path_from_uri(cursor.active_project_uri)
        locations[str(path)] = path.parent.parent
        if cursor.predecessor_authority_head_sha256 is None:
            break
        prior = load_head(cursor.predecessor_authority_head_sha256)
        if prior.authority_head_sha256 != cursor.predecessor_authority_head_sha256:
            raise BatchStateError("project location predecessor differs from its recorded digest")
        if cursor.active_project_uri != prior.active_project_uri:
            if prior.successor(active_project_uri=cursor.active_project_uri) != cursor:
                raise BatchStateError("project relocation changes more than its location")
            relocations.append((prior.active_project_uri, cursor.active_project_uri))
        cursor = prior
    return heads, tuple(relocations), locations


def project_saved_states(
    *,
    catalogue: BatchProjectCatalogue,
    current: ProjectAuthorityHead,
    project_identity_sha256: str,
    load_head: Callable[[str], ProjectAuthorityHead],
    load_attestation: Callable[[str], ProjectMutationAttestation],
    load_stage_evidence: Callable[[str], ProjectStageMutationEvidenceRecord],
    validate_native_fit: Callable[[ProjectAuthorityHead, ProjectMutationAttestation, str], None],
    annotation_transitions: Mapping[tuple[str, str], str],
    load_catalogue: Callable[[str], BatchProjectCatalogue] | None = None,
    relocations: tuple[tuple[str, str], ...] = (),
) -> dict[str, str]:
    """Reconstruct each image's saved hierarchy from its accepted changes.

    We start with each image's most recent upsert hash, then apply accepted
    stage observations and annotation publications in order. A later upsert
    for another image retains the earlier image's saved-state history. Callers
    provide authenticated record readers and the selected project head. Each
    observation must join the preceding saved hash for the same image.

    ``relocations`` contains only exact URI-only successors in the authenticated
    project history. A matching pair permits the location change produced by
    import; it does not permit changes to image entries,
    project bytes or scientific mutation records. We never read or change image
    pixels or saved hierarchy files here.
    """
    from .batch_project import BatchProjectCatalogue
    from .batch_state import BatchStateError

    values: dict[str, str] = {}
    for entry in catalogue.entries:
        if entry.saved_state_attestation_sha256 is None or entry.project_entry_id in values:
            raise BatchStateError("saved-state projection catalogue is ambiguous")
        values[entry.project_entry_id] = entry.saved_state_attestation_sha256
    annotation_transitions = dict(annotation_transitions)
    evidence_history = []
    tracked_entries = set(values)
    cursor = current
    visited: set[str] = set()
    while True:
        if cursor.authority_head_sha256 in visited:
            raise BatchStateError("saved-state projection authority is cyclic")
        visited.add(cursor.authority_head_sha256)
        prior = None
        if relocations and cursor.predecessor_authority_head_sha256 is not None:
            prior = load_head(cursor.predecessor_authority_head_sha256)
            if cursor.active_project_uri != prior.active_project_uri:
                if (
                    (prior.active_project_uri, cursor.active_project_uri) not in relocations
                    or prior.successor(active_project_uri=cursor.active_project_uri) != cursor
                ):
                    raise BatchStateError(
                        "saved-state project relocation differs from its accepted receipt"
                    )
                cursor = prior
                continue
        attestation_sha256 = cursor.latest_mutation_attestation_sha256
        if attestation_sha256 is None:
            raise BatchStateError("saved-state projection lacks catalogue baseline")
        attestation = load_attestation(attestation_sha256)
        predecessor_sha256 = attestation.predecessor_authority_head_sha256
        if (
            attestation.project_identity_sha256 != project_identity_sha256
            or predecessor_sha256 is None
            or cursor.predecessor_authority_head_sha256 != predecessor_sha256
            or attestation.post_save_qpproj_snapshot_sha256 != cursor.qpproj_snapshot_sha256
        ):
            raise BatchStateError("saved-state projection authority is discontinuous")
        if attestation.mutation_kind == "batch_catalogue_upsert":
            while catalogue.catalogue_sha256 != attestation.workspace_catalogue_sha256:
                if catalogue.predecessor_catalogue_sha256 is None or load_catalogue is None:
                    raise BatchStateError("saved-state projection catalogue baseline differs")
                prior_catalogue = load_catalogue(catalogue.predecessor_catalogue_sha256)
                if prior_catalogue.project_uri == catalogue.project_uri:
                    break
                expected = BatchProjectCatalogue.create(
                    batch_id=prior_catalogue.batch_id,
                    project_identity_sha256=prior_catalogue.project_identity_sha256,
                    project_uri=catalogue.project_uri,
                    project_authority_predecessor_head_sha256=prior_catalogue.project_authority_predecessor_head_sha256,
                    qpproj_snapshot_sha256=prior_catalogue.qpproj_snapshot_sha256,
                    predecessor_catalogue_sha256=prior_catalogue.catalogue_sha256,
                    mutation_request_sha256=prior_catalogue.mutation_request_sha256,
                    mutation_authorization_sha256=prior_catalogue.mutation_authorization_sha256,
                    entries=prior_catalogue.entries,
                )
                if (
                    catalogue != expected
                    or (prior_catalogue.project_uri, catalogue.project_uri) not in relocations
                ):
                    raise BatchStateError(
                        "saved-state catalogue relocation differs from its accepted receipt"
                    )
                catalogue = prior_catalogue

            if (
                attestation.workspace_catalogue_sha256 != catalogue.catalogue_sha256
                or cursor.qpproj_snapshot_sha256 != catalogue.qpproj_snapshot_sha256
            ):
                raise BatchStateError("saved-state projection catalogue baseline differs")
            if catalogue.predecessor_catalogue_sha256 is None:
                break
            if load_catalogue is None:
                raise BatchStateError("saved-state projection lacks its catalogue predecessor")
            earlier = load_catalogue(catalogue.predecessor_catalogue_sha256)
            if (earlier.catalogue_sha256 != catalogue.predecessor_catalogue_sha256
                    or earlier.project_identity_sha256 != catalogue.project_identity_sha256
                    or earlier.batch_id != catalogue.batch_id):
                raise BatchStateError("saved-state catalogue predecessor differs")
            earlier_entries = {entry.project_entry_id: entry for entry in earlier.entries}
            tracked_entries.intersection_update(
                entry.project_entry_id for entry in catalogue.entries
                if earlier_entries.get(entry.project_entry_id) == entry
            )
            if not tracked_entries:
                break
            catalogue = earlier
            cursor = prior if prior is not None else load_head(predecessor_sha256)
            continue
        if attestation.mutation_kind == "project_stage":
            evidence = load_stage_evidence(attestation.operation_evidence_sha256)
            if (
                evidence.mutation_evidence_sha256 != attestation.operation_evidence_sha256
                or evidence.project_identity_sha256 != project_identity_sha256
                or evidence.predecessor_project_authority_head_sha256 != predecessor_sha256
                or evidence.mutation_request_sha256 != attestation.mutation_request_sha256
                or evidence.mutation_authorization_sha256
                != attestation.mutation_authorization_sha256
            ):
                raise BatchStateError("saved-state project-stage evidence differs")
            evidence_history.append((evidence, frozenset(tracked_entries)))
        elif attestation.mutation_kind == "annotation_classifier":
            validate_native_fit(cursor, attestation, predecessor_sha256)
        else:
            raise BatchStateError("saved-state projection mutation kind is unsupported")
        cursor = prior if prior is not None else load_head(predecessor_sha256)

    def advance_annotations(entry_id: str, target: str | None = None) -> None:
        """Consume accepted imports up to an observation's saved baseline.

        Args:
            entry_id: Catalogue image entry whose saved state we advance.
            target: Requested predecessor hash; omit to apply every
                remaining publication connected to this entry.

        Returns:
            None. We update the projected hash and consume each receipt
            once, leaving unrelated transitions for their owning entry.

        Raises:
            BatchStateError: The publication chain revisits a saved state.
        """
        visited: set[str] = set()
        while values[entry_id] != target:
            if values[entry_id] in visited:
                raise BatchStateError("annotation publications contain a saved-state cycle")
            visited.add(values[entry_id])
            after = annotation_transitions.pop((entry_id, values[entry_id]), None)
            if after is None:
                break
            values[entry_id] = after

    for evidence, tracked in reversed(evidence_history):
        for observation in evidence.target_observations:
            if observation.project_entry_id not in tracked:
                continue
            advance_annotations(
                observation.project_entry_id, observation.saved_state_before_sha256
            )
            before = values.get(observation.project_entry_id)
            after = observation.saved_state_after_sha256
            if (
                before != observation.saved_state_before_sha256
                or observation.saved_state_disposition == "unattestable"
                or after is None
            ):
                raise BatchStateError("saved-state projection observation differs")
            # A retained observation may already include the accepted
            # import. Consume that same transition once, without replay.
            key = (observation.project_entry_id, before)
            if annotation_transitions.get(key) == after:
                annotation_transitions.pop(key)
            values[observation.project_entry_id] = after
    for entry_id in values:
        advance_annotations(entry_id)
    if annotation_transitions:
        raise BatchStateError("annotation publication does not join saved-state history")
    return dict(sorted(values.items()))
