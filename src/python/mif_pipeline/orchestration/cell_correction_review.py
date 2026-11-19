"""Prepare and accept known-cell drafts through the existing revision owner."""
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname
import hashlib
import uuid

from mif_pipeline.pipeline_core import StageStatus
from .batch_cell_corrections import (
    CellCorrectionRequest, correction_operations, current_cell_correction, decode_committed_cell_correction,
    correction_domain_outlines, read_correction_submission, current_measurement_geometry,
)
from .cluster.cluster_annotations import _folder, _sha, _new, _read, _encoded, _sync, _current_project_sha, run_annotation_script


@contextmanager
def _targets(service, batch_id, artifact_refs, expected_head):
    """Bind all selected images to one accepted snapshot and writer lease."""
    service._require_batch(batch_id)
    store = service._dependencies.runtime.state_store
    with store.acquire_writer_lease("known-cell-correction") as lease:
        with service._lock:
            if service._active_wave is not None:
                raise ValueError("Wait for processing to stop before correcting a cell.")
        snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_head)
        catalogue = store.reopen_current_project_catalogue(expected_head_sha256=expected_head)
        saved = store.current_project_entry_saved_state_sha256_by_id(expected_head_sha256=expected_head, lease=lease)
        targets = tuple(_target_details(store, snapshot, catalogue, saved, artifact)
                        for artifact in artifact_refs)
        yield store, lease, snapshot, targets


def _target_details(store, snapshot, catalogue, saved, artifact_ref_sha256):
    authority = next((item for item in snapshot.committed_artifact_authorities
                      if item.artifact_ref.artifact_kind == "segmentation_result"
                      and item.artifact_ref.artifact_ref_sha256 == artifact_ref_sha256), None)
    if authority is None or snapshot.head.pending_authorization_sha256_by_node:
        raise ValueError("Open the current accepted segmentation before correcting a cell.")
    ref = authority.artifact_ref
    section_nodes = [node for node in snapshot.plan.nodes
                     if node.section_target is not None
                     and node.section_target.section_id == ref.owning_identity["section_id"]
                     and node.participant_ids == (ref.scope.sample_id,)]
    if any(node.kind == "measurements" and snapshot.head.node_status_by_id.get(node.node_id) is StageStatus.FAILED
           for node in section_nodes):
        raise ValueError("Finish the stopped measurement task using its cell-removal review and Retry before opening further corrections.")
    if any(node.kind == "segmentation_correction" and snapshot.head.node_status_by_id.get(node.node_id) is not StageStatus.COMPLETED
           for node in section_nodes):
        raise ValueError("Finish the current cell correction before opening another draft.")
    measurements = [node for node in section_nodes if node.kind == "measurements"]
    predecessor = measurements[0] if measurements else next(
        node for node in section_nodes if node.node_id == ref.producer_node_id)
    correction = current_cell_correction(snapshot, ref)
    measurement, _selection = current_measurement_geometry(snapshot, ref, correction)
    if (predecessor.kind == 'measurements'
            and snapshot.head.node_status_by_id.get(predecessor.node_id) is StageStatus.COMPLETED
            and (measurement is None or measurement.artifact_ref.producer_node_id != predecessor.node_id)):
        raise ValueError('Reopen the current accepted measurements before correcting a cell.')
    entry = catalogue.resolve(ref.scope.project_entry_id)
    source = Path(snapshot.head.project_path).parent / "data" / entry.project_entry_id / "data.qpdata"
    source_sha = _sha(source)
    if source_sha != saved[entry.project_entry_id]:
        raise ValueError("The saved cells changed. Reopen the current segmentation review.")
    return authority, correction, predecessor, entry, source_sha


@contextmanager
def _target(service, batch_id, artifact_ref_sha256, expected_head):
    with _targets(service, batch_id, (artifact_ref_sha256,), expected_head) as (store, lease, snapshot, targets):
        yield store, lease, snapshot, *targets[0]


def prepare_correction(service, batch_id, *, artifact_ref_sha256, expected_head_sha256,
                       cell_id=None, operation=None, operations=None, resume_selection=None, review_bounds_level0=None):
    from .saved_segmentation_cells import segmentation_cells, segmentation_area_geometry
    from .saved_cell_gallery import gallery_resources
    from mif_pipeline.datasets.geometry import polygon_wkb

    with _target(service, batch_id, artifact_ref_sha256, expected_head_sha256) as (
            store, _lease, snapshot, authority, correction, predecessor, entry, source_sha):
        if operations is not None and (cell_id is not None or operation is not None):
            raise ValueError("Submit the complete correction list once.")
        operations = correction_operations(operations if operations is not None else
            [dict(cell_id=cell_id, operation=operation)])
        resumed = None
        if resume_selection is not None:
            previous = CellCorrectionRequest.from_mapping(resume_selection)
            resumed = read_correction_submission(store, previous)
            if (previous.segmentation_result_ref_sha256 != artifact_ref_sha256
                    or previous.previous_correction_ref_sha256 != (None if correction is None else correction.artifact_ref.artifact_ref_sha256)
                    or previous.operations != operations
                    or resumed['state_head_sha256'] != snapshot.head.sha256
                    or resumed['source_sha256'] != source_sha):
                raise ValueError('This draft no longer matches the current cells and queued corrections.')
        base = segmentation_cells(authority, store)
        measurement, selected = current_measurement_geometry(snapshot, authority.artifact_ref, correction)
        if selected is None and correction is not None:
            selected = decode_committed_cell_correction(correction, authority.artifact_ref)
        scope = {key: authority.artifact_ref.owning_identity[key]
                 for key in ("section_id", "segmentation_population_id", "cell_evidence_sha256")}
        scope.update(operations=[item.to_mapping() for item in operations],
                     cell_boundary_source=base.description["scope"].get("cell_boundary_source"),
                     geometry_revision=0 if selected is None else selected["geometry_revision"],
                     cell_edits=None if selected is None or selected['cell_edits'] is None else dict(selected["cell_edits"]))
        if review_bounds_level0 is not None:
            import math
            bounds=review_bounds_level0
            if (not isinstance(bounds,(list,tuple)) or len(bounds)!=4
                    or any(type(value) not in (int,float) or not math.isfinite(value) for value in bounds)
                    or bounds[2]<=bounds[0] or bounds[3]<=bounds[1]):
                raise ValueError('Open a valid image field before editing outlines.')
            scope['review_bounds_level0']=list(map(float,bounds))
        if measurement is not None:
            scope['measurement_source'] = dict(terminal_record_sha256=measurement.terminal_record_sha256,
                artifact_ref_sha256=measurement.artifact_ref.artifact_ref_sha256)
        with gallery_resources(snapshot, authority, memory_bytes=768*1024**2):
            if any(item.operation != "delete" for item in operations):
                area = segmentation_area_geometry(authority, snapshot, service._dependencies, service._workspace_layout.summaries)
                scope.update(analysis_area_wkb_hex=polygon_wkb(area)[0].hex(),
                             union_geometry_sha256=authority.artifact_ref.owning_identity["union_geometry_sha256"],
                             domains=correction_domain_outlines(authority, snapshot, store))
            folder = _folder(service) / uuid.uuid4().hex
            folder.mkdir(mode=0o700)
            native = run_annotation_script(service._dependencies.runtime.runner, folder, {
                "mode": "export", "project_file": snapshot.head.project_path,
                "project_sha256": _current_project_sha(snapshot.head.project_path, snapshot.head.project_qpproj_snapshot_sha256),
                "entry_id": entry.project_entry_id, "cell_correction": scope})
        image = urlparse(entry.canonical_uri)
        if image.scheme != "file" or image.netloc or native["source_sha256"] != source_sha or native["image_uri"] != entry.canonical_uri:
            raise ValueError("The correction draft differs from its accepted image.")
        if resumed is not None:
            # Start the new local draft from this exact, previously validated
            # return. The accepted source and the before geometry stay unchanged.
            native = {**native, 'correction_outline': resumed['outline']}
        ticket = dict(schema_version="cluster-annotation-ticket/v1", ticket_id=folder.name,
            batch_id=batch_id, request_id=None, state_head_sha256=snapshot.head.sha256,
            plan_sha256=snapshot.plan.plan_sha256, predecessor_node_id=predecessor.node_id,
            segmentation_result_ref_sha256=artifact_ref_sha256,
            previous_correction_ref_sha256=None if correction is None else correction.artifact_ref.artifact_ref_sha256,
            interaction_context_sha256=hashlib.sha256(_encoded(scope)).hexdigest(),
            image_sha256=entry.image_sha256, image_path=url2pathname(image.path),
            project_file=snapshot.head.project_path, cell_correction=scope, **native)
        if measurement is not None:
            ticket['revision_preview'] = dict(affected_node_ids=sorted(_affected_nodes(snapshot, predecessor)),
                sample_id=authority.artifact_ref.scope.sample_id)
        _new(folder / "ticket.json", ticket); _sync(folder)
        return ticket


def prepare_correction_candidate(service, batch_id, *, ticket_id, objects=None):
    if not isinstance(ticket_id, str) or len(ticket_id) != 32 or set(ticket_id) - set("0123456789abcdef"):
        raise ValueError("Open a current cell correction draft.")
    folder = _folder(service) / ticket_id
    ticket = _read(folder / "ticket.json")
    with _target(service, batch_id, ticket["segmentation_result_ref_sha256"], ticket["state_head_sha256"]) as (
            _store, _lease, _snapshot, _authority, _correction, predecessor, _entry, source_sha):
        if ticket["batch_id"] != batch_id or predecessor.node_id != ticket["predecessor_node_id"] or source_sha != ticket["source_sha256"]:
            raise ValueError("The cell correction draft is no longer current.")
        if objects is None:
            if any(item["operation"] != "delete" for item in ticket["cell_correction"]["operations"]):
                raise ValueError("Return the edited outline from its native draft.")
            objects = dict(ticket["snapshot"]["objects"], mif_cell_selection=ticket["cell_selection"])
        result = run_annotation_script(service._dependencies.runtime.runner, folder, {
            "mode": "cell-correction-preview", "project_file": ticket["project_file"],
            "project_sha256": _current_project_sha(ticket["project_file"], ticket["project_sha256"]),
            "entry_id": ticket["entry_id"], "source_sha256": source_sha,
            "cell_correction": ticket["cell_correction"], "correction_outline": ticket["correction_outline"],
            "snapshot": ticket["snapshot"], "cell_selection": ticket["cell_selection"], "objects": objects})
        body = {key: ticket[key] for key in ("ticket_id", "state_head_sha256", "plan_sha256", "predecessor_node_id",
                "segmentation_result_ref_sha256", "previous_correction_ref_sha256", "source_sha256")}
        if not result["preview"]["changes"]:
            return {"selection": None, "preview": result["preview"], "ticket": ticket}
        body.update(schema_version="mif-known-cell-correction/v2", scope=ticket["cell_correction"],
                    preview=result["preview"], outline=result["outline"])
        candidate = folder / "correction.json"
        if candidate.exists():
            if _read(candidate) != body:
                raise ValueError("This draft already has a different reviewed outline. Open a new correction draft.")
        else:
            _new(candidate, body); _sync(folder)
        selected = CellCorrectionRequest(predecessor.node_id, ticket["segmentation_result_ref_sha256"],
            ticket["previous_correction_ref_sha256"], correction_operations(ticket["cell_correction"]["operations"]),
            ticket_id, hashlib.sha256(_encoded(body)).hexdigest())
        return {"selection": selected.to_mapping(), "preview": result["preview"], "ticket": ticket}


def _affected_nodes(snapshot, predecessor):
    from .batch_dag import dependent_node_ids
    return (dependent_node_ids(((n.node_id, n.dependency_ids) for n in snapshot.plan.nodes), {predecessor.node_id})
            if predecessor.kind == 'measurements' else set())


def accept_correction(service, batch_id, *, selected, expected_head_sha256):
    return accept_corrections(service, batch_id, selections=(selected,),
                              expected_head_sha256=expected_head_sha256)


def accept_corrections(service, batch_id, *, selections, expected_head_sha256):
    """Admit the reviewed image drafts together through one plan revision."""
    from .batch_dag import expand_batch_plan
    from .batch_service_dependencies import build_batch_stage_registry
    from .batch_pause import consume_quiescent_pause
    from .batch_state import BatchExecutionState
    from .setup.batch_prerequisites import bind_batch_prerequisites
    from .batch_saved_result_retirement import SavedResultRetirement, DISPLAY_KINDS

    selected = tuple(CellCorrectionRequest.from_mapping(item) for item in selections)
    if not selected:
        raise ValueError("Choose at least one reviewed correction.")
    artifacts = tuple(item.segmentation_result_ref_sha256 for item in selected)
    if len(set(artifacts)) != len(artifacts):
        raise ValueError("Review each section's corrections once before accepting them.")
    if len({item.predecessor_node_id for item in selected}) != len(selected):
        raise ValueError("Review each section's corrections once before accepting them.")
    with _targets(service, batch_id, artifacts, expected_head_sha256) as (store, lease, snapshot, targets):
        affected_by_predecessor = {}
        for request, (_authority, _correction, predecessor, _entry, source_sha) in zip(selected, targets):
            body = read_correction_submission(store, request)
            if (body["state_head_sha256"] != expected_head_sha256 or body["source_sha256"] != source_sha
                    or predecessor.node_id != request.predecessor_node_id):
                raise ValueError("A prepared correction is no longer current. Reopen its image review.")
            affected_by_predecessor[predecessor.node_id] = _affected_nodes(snapshot, predecessor)
        # Shared phenotype results are retired once. A corrected image's own
        # predecessor owns its cleanup so that its correction waits for it.
        affected = set().union(*affected_by_predecessor.values())
        display_nodes = {node.node_id for node in snapshot.plan.nodes
                         if node.node_id in affected and node.kind in DISPLAY_KINDS}
        predecessor_by_section = {
            (target[3].project_entry_id, target[0].artifact_ref.owning_identity["section_id"]): request.predecessor_node_id
            for target, request in zip(targets, selected)}
        sources_by_owner = {}
        for ref in sorted(snapshot.accepted_artifacts, key=lambda item: item.artifact_ref_sha256):
            if ref.producer_node_id not in display_nodes:
                continue
            eligible = [node_id for node_id, closure in affected_by_predecessor.items()
                        if ref.producer_node_id in closure]
            owner = predecessor_by_section.get((ref.scope.project_entry_id, ref.owning_identity.get("section_id")))
            if owner not in eligible:
                owner = eligible[0]
            sources_by_owner.setdefault((owner, ref.scope.project_entry_id), []).append(ref)
        retirements = tuple(SavedResultRetirement(owner, snapshot.plan.plan_sha256, tuple(refs))
                            for (owner, _entry), refs in sorted(sources_by_owner.items()))
        retained = tuple(ref for ref in snapshot.accepted_artifacts if ref.producer_node_id not in affected)
        retained_refs = {ref.artifact_ref_sha256 for ref in retained}
        registry = build_batch_stage_registry(plan=snapshot.plan, resolved_samples=snapshot.manifest.enabled_samples,
                                              dependencies=service._dependencies)
        successor = expand_batch_plan(snapshot.plan, snapshot.manifest, registry.stage_registry, retained,
            accepted_artifact_authorities=tuple(item for item in snapshot.committed_artifact_authorities
                if item.artifact_ref.artifact_ref_sha256 in retained_refs), batch_state_store=store,
            training_plan_set_store=service._dependencies.training.training_plan_set_store,
            saved_result_retirements=retirements, cell_corrections=selected)
        preserved = set(store.preview_reusable_completed_nodes(successor, expected_head_sha256=expected_head_sha256))
        required = {n.kind for n in successor.nodes if n.node_id not in preserved} | {n.node_kind for n in successor.deferred_nodes}
        successor = bind_batch_prerequisites(successor, service._collect_prerequisites(
            snapshot.manifest, successor, required_node_kinds=frozenset(required)))
        service._admit_prerequisites(successor.prerequisites)
        if not service._dependencies.runtime.wait_for_project_stage_quiescence():
            raise ValueError("Wait for the current native operation to exit before accepting the corrections.")
        with store.current_project_control(operation="revise-options", lease=lease) as control:
            control.project_processes.assert_all_drained(batch_id=batch_id, project_identity_sha256=snapshot.head.project_identity_sha256)
            if snapshot.head.execution_state is BatchExecutionState.PAUSED:
                consume_quiescent_pause(state_store=store, batch_id=batch_id,
                    pause_record_sha256=snapshot.head.quiescent_pause_sha256,
                    scheduler=service._new_scheduler(snapshot, lease, registry), workspace_lease=lease,
                    project_lease=control.project_lease, project_processes=control.project_processes)
            store.revise_plan(successor, "reviewed cell corrections", store.load_head().sha256, lease)
        with service._lock:
            service._pause_blocks_admission = False
        return service._status_from_snapshot(store.reopen_execution_snapshot())
