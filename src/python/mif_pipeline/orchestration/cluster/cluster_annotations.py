"""Exchange native annotation drafts with the current local or cluster review.

The export records the selected image and its protected hierarchy. Local QuPath edits a
separate draft and returns candidate annotations under that exact ticket. The import
validates the current request, image/frame and permitted changes, then publishes the updated
saved hierarchy while retaining its predecessor."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.request import url2pathname
from urllib.parse import urlparse, unquote
import hashlib, json, os, re, uuid

from mif_pipeline.qupath_runtime.project_content import same_project_snapshot
from mif_pipeline.qupath_runtime.project_pyramid import retained_project_file

if TYPE_CHECKING:
    from mif_pipeline.pipeline_core import StageResult
    from ..batch_project import BatchProjectEntry

MAX_JSON = 32 * 1024 * 1024
MAX_HIERARCHY = 256 * 1024 * 1024


# ==============================================================================
#                              Cluster annotations
# ==============================================================================


def _sha(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("The saved annotation file is unavailable or linked.")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > MAX_HIERARCHY:
            raise ValueError("The saved hierarchy exceeds this review’s size limit.")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
        named = path.stat()
    identity = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    if identity(before) != identity(after) or identity(after) != identity(named):
        raise ValueError("The saved annotations changed while being read.")
    return digest


def _current_project_sha(project_file, expected_sha256):
    """Keep the saved project binding while pinning this operation's current bytes."""
    project = Path(project_file)
    with retained_project_file(project) as retained:
        if not same_project_snapshot(
            project, expected_sha256, retained.sha256, observed_payload=retained.payload,
        ):
            raise ValueError("The image inputs in the project changed. Reopen the current review.")
        retained.validate_unchanged()
        return retained.sha256


def _encoded(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > MAX_JSON:
        raise ValueError("The annotation review exceeds its size limit.")
    return data


def _read(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Repeated annotation request field.")
            result[key] = value
        return result

    path = Path(path)
    if path.is_symlink() or path.stat().st_size > MAX_JSON:
        raise ValueError("The annotation request is unavailable or too large.")
    return json.loads(
        path.read_text(),
        object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError("Nonfinite annotation value.")
        ),
    )


def _new(path, value):
    data = _encoded(value)
    with Path(path).open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _sync(folder):
    if os.name == "posix":
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _copy_checked(source, destination, expected):
    temporary = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with Path(source).open("rb") as incoming, temporary.open("xb") as outgoing:
            count = 0
            while block := incoming.read(1024 * 1024):
                count += len(block)
                if count > MAX_HIERARCHY:
                    raise ValueError("The annotation hierarchy exceeds the limit.")
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if _sha(temporary) != expected or _sha(source) != expected:
            raise ValueError("The candidate or original annotations changed during publication.")
        if destination.exists():
            if _sha(destination) != expected:
                raise ValueError("A different saved annotation record already exists.")
        else:
            os.replace(temporary, destination)
        _sync(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def publish_candidate(original, candidate, journal, before_sha256, after_sha256):
    """Publish under the caller's exclusive batch writer; retain the original file."""
    original, candidate, journal = map(Path, (original, candidate, journal))
    if journal.is_symlink() or not journal.is_dir():
        raise ValueError("The review folder is unavailable.")
    if original.resolve() == candidate.resolve():
        raise ValueError("The candidate must be separate from the saved hierarchy.")
    if _sha(candidate) != after_sha256:
        raise ValueError("The annotation candidate has changed.")
    current = _sha(original)
    intent = {
        "schema_version": "cluster-annotation-publication/v1",
        "file": str(original),
        "previous_sha256": before_sha256,
        "accepted_sha256": after_sha256,
    }
    intent_path = journal / "publication.json"
    receipt = journal / "accepted.json"
    if intent_path.exists():
        if _read(intent_path) != intent:
            raise ValueError("This review already contains a different candidate.")
    else:
        if current != before_sha256:
            raise ValueError("The saved annotations changed before import.")
        _new(intent_path, intent)
        _sync(journal)
    if receipt.exists():
        if _read(receipt) != intent or current != after_sha256:
            raise ValueError("The saved annotations changed after this review was accepted.")
        return intent
    previous = journal / "previous.qpdata"
    if current == after_sha256 and previous.exists() and _sha(previous) == before_sha256:
        _new(receipt, intent)
        _sync(journal)
        return intent
    if current != before_sha256:
        raise ValueError("The saved annotations changed before import.")
    _copy_checked(original, previous, before_sha256)
    temporary = original.with_name(".cluster-annotations-" + uuid.uuid4().hex + ".qpdata")
    try:
        _copy_checked(candidate, temporary, after_sha256)
        if _sha(original) != before_sha256:
            raise ValueError("Another editor changed the saved annotations.")
        os.replace(temporary, original)
        _sync(original.parent)
        _new(receipt, intent)
        _sync(journal)
    finally:
        temporary.unlink(missing_ok=True)
    return intent


def _folder(service):
    folder = service._workspace_layout.root / "cluster-reviews" / "annotations"
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink():
        raise ValueError("The cluster review folder must belong to this analysis.")
    return folder


def annotation_publication_records(workspace_root: Path) -> dict[str, bytes]:
    """Read the four existing records for each completed annotation import.

    Candidate and previous hierarchies are not needed to reopen an accepted
    publication. Their hashes were recorded when the import completed.
    """
    root = Path(workspace_root) / "cluster-reviews" / "annotations"
    if not root.exists():
        return {}
    if root.is_symlink() or not root.is_dir():
        raise ValueError("The annotation publication folder is unavailable.")
    records = {}
    for folder in sorted(root.iterdir()):
        if folder.is_symlink() or not folder.is_dir() or not re.fullmatch("[0-9a-f]{32}", folder.name):
            raise ValueError("The annotation review folder is invalid.")
        for receipt in sorted(folder.glob("*/accepted.json")):
            if receipt.parent.is_symlink() or not re.fullmatch("[0-9a-f]{64}", receipt.parent.name):
                raise ValueError("The annotation publication folder is invalid.")
            for path in (folder / "ticket.json", receipt, receipt.parent / "candidate.json",
                         receipt.parent / "publication.json"):
                value = _read(path)
                records[path.relative_to(root).as_posix()] = _encoded(value)
    return records


def accepted_annotation_transitions(
    workspace_root: Path,
    *,
    batch_id: str,
    project_file: str | Path,
    entries: tuple[BatchProjectEntry, ...],
    load_interaction_request: Callable[[str], StageResult],
    load_attempt: Callable | None = None,
    records: Mapping[str, bytes] | None = None,
    original_workspace_root: Path | None = None,
    project_locations: Mapping[str, Path] | None = None,
) -> dict[tuple[str, str], str]:
    """Reopen our accepted annotation publications for saved-state projection.

    We bind each small publication receipt to its native candidate, review and
    image entry. Import already checked the hierarchy bytes before publication;
    subsequent projections read these records without rehashing image hierarchies.

    Args:
        workspace_root: Analysis directory containing our annotation reviews.
        batch_id: Current analysis identity used to select its publications.
        project_file: Project location recorded when these annotations were saved.
            For an imported analysis, its receipt supplies the original location.
        entries: Current catalogue entries providing image and entry identities.
        load_interaction_request: Owner reader for the retained review request;
            its context must match the request that authorized annotation import.
        records: Exact retained publication bytes supplied by capsule inspection;
            otherwise we read the existing publication files in the analysis.
        original_workspace_root: Original analysis directory from the import
            receipt, used to check unchanged paths inside retained tickets.
        project_locations: Additional authenticated project/analysis locations,
            including the current destination for annotations saved after import.

    Returns:
        Accepted successor hashes keyed by image entry and predecessor hash.
        Reviews without a completed publication contribute no transition.

    Raises:
        ValueError: A publication has ambiguous ancestry or differs from its
            review, image, source hierarchy or prepared native candidate.
        OSError: A retained publication record cannot be read.
    """
    root = Path(workspace_root) / "cluster-reviews" / "annotations"
    locations = {str(project_file): Path(original_workspace_root or workspace_root)}
    if project_locations is not None:
        locations.update(project_locations)
    records = annotation_publication_records(workspace_root) if records is None else records
    by_entry = {entry.project_entry_id: entry for entry in entries}
    transitions: dict[tuple[str, str], str] = {}
    expected = set()

    def read(name):
        value = json.loads(records[name])
        if _encoded(value) != records[name]:
            raise ValueError("The annotation publication record is not canonical.")
        return value

    folders = sorted({name.split("/")[0] for name in records})
    for ticket_id in folders:
        if not re.fullmatch("[0-9a-f]{32}", ticket_id):
            raise ValueError("The annotation review folder is invalid.")
        folder = root / ticket_id
        receipt_names = sorted(name for name in records
                               if name.startswith(ticket_id + "/") and name.endswith("/accepted.json"))
        if not receipt_names:
            raise ValueError("The annotation publication lacks its accepted receipt.")
        expected.add(ticket_id + "/ticket.json")
        ticket = read(ticket_id + "/ticket.json")
        entry = by_entry.get(ticket.get("entry_id"))
        removal = ticket.get("cell_removal")
        if removal is not None:
            if load_attempt is None:
                raise ValueError("The cell removal publication lacks its measurement owner.")
            attempt = load_attempt(removal["terminal_record_sha256"])
            _validate_removal_owner(attempt, removal, ticket.get("entry_id"), ticket.get("image_sha256"))
            context_sha = hashlib.sha256(_encoded(removal)).hexdigest()
        else:
            request = load_interaction_request(ticket.get("request_id"))
            context_sha = hashlib.sha256(_encoded(request.to_mapping()["resume_context"])).hexdigest()
        if (
            ticket.get("schema_version") != "cluster-annotation-ticket/v1"
            or ticket.get("ticket_id") != folder.name
            or ticket.get("batch_id") != batch_id
            or ticket.get("project_file") not in locations
            or entry is None
            or entry.image_role != "primary_mif"
            or ticket.get("image_sha256") != entry.image_sha256
            or ticket.get("interaction_context_sha256") != context_sha
        ):
            raise ValueError("The annotation publication differs from its review or image.")
        ticket_project = ticket["project_file"]
        original_root = locations[ticket_project] / "cluster-reviews" / "annotations"
        source = str(Path(ticket_project).parent / "data" / entry.project_entry_id / "data.qpdata")
        for receipt_name in receipt_names:
            receipt_path = root / receipt_name
            attempt = receipt_path.parent
            if attempt.is_symlink() or not re.fullmatch("[0-9a-f]{64}", attempt.name):
                raise ValueError("The annotation publication folder is invalid.")
            prefix = attempt.relative_to(root).as_posix()
            expected.update(prefix + "/" + name for name in ("accepted.json", "candidate.json", "publication.json"))
            receipt = read(receipt_name)
            candidate = read(prefix + "/candidate.json")
            before, after = receipt.get("previous_sha256"), receipt.get("accepted_sha256")
            if (
                receipt != read(prefix + "/publication.json")
                or receipt != {"schema_version": "cluster-annotation-publication/v1",
                               "file": source, "previous_sha256": before, "accepted_sha256": after}
                or not isinstance(before, str) or not re.fullmatch("[0-9a-f]{64}", before)
                or not isinstance(after, str) or not re.fullmatch("[0-9a-f]{64}", after)
                or ticket.get("source_file") != source
                or ticket.get("source_sha256") != before
                or candidate.get("source_file") != source
                or candidate.get("source_sha256") != before
                or candidate.get("candidate_file") != str(original_root / prefix / "candidate.qpdata")
                or candidate.get("candidate_sha256") != after
            ):
                raise ValueError("The accepted annotation publication differs from its candidate.")
            if before == after:
                continue
            key = (entry.project_entry_id, before)
            if key in transitions:
                raise ValueError("The annotation publications have an ambiguous predecessor.")
            transitions[key] = after
    if set(records) != expected:
        raise ValueError("The annotation publication inventory contains unrelated records.")
    return transitions


def _support(folder):
    from mif_pipeline.qupath_runtime.project import ProjectLayout
    from mif_pipeline.qupath_runtime.scripts import materialize_script_bundle, bundle_script_path, QuPathScriptAsset

    root = folder / "support"
    root.mkdir(parents=True, exist_ok=True)
    layout = ProjectLayout(
        root,
        root / "project.qpproj",
        root / "scripts",
        root / "pixel",
        root / "object",
        root / "data",
        root / "exchange",
        root / "checkpoints",
        root / "provenance",
        root / "runs",
        root / "config.json",
        "{}",
    )
    bundle = materialize_script_bundle(
        layout, (QuPathScriptAsset.CLUSTER_ANNOTATIONS, QuPathScriptAsset.CLUSTER_ANNOTATION_ENTRY,
                 QuPathScriptAsset.SUPERVISED_REFERENCES)
    )
    return bundle_script_path(bundle, QuPathScriptAsset.CLUSTER_ANNOTATIONS), bundle_script_path(
        bundle, QuPathScriptAsset.CLUSTER_ANNOTATION_ENTRY
    ), bundle_script_path(bundle, QuPathScriptAsset.SUPERVISED_REFERENCES)


def run_annotation_script(runner, folder, request):
    helper, script, reference_helper = _support(folder)
    request = {**request, "helper_file": str(helper), "helper_sha256": _sha(helper),
               "reference_helper_file": str(reference_helper), "reference_helper_sha256": _sha(reference_helper)}
    operation = folder / uuid.uuid4().hex
    operation.mkdir()
    request_path = operation / "request.json"
    result_path = operation / "result.json"
    _new(request_path, request)
    runner.run_evidence_script(
        project_file=request.get("project_file", operation / "local.qpproj"),
        script=script,
        request_paths=(request_path,),
        log_directory=operation / "logs",
        require_existing_project=False,
        evidence_path=result_path,
    )
    return _read(result_path)


def annotation_entry_ids(target):
    """Use only image identities already declared by the current review owner."""
    identities = set()
    if target.project_entry_id is not None:
        identities.add(target.project_entry_id)
    identities.update(binding.project_entry_id for binding in target.analysis_domain_bindings)
    context = target.stage_result.resume_context or {}
    candidates = context.get("candidate_entry_ids", {})
    from collections.abc import Mapping

    if not isinstance(candidates, Mapping):
        raise ValueError("The current annotation candidate list is invalid.")
    for entries in candidates.values():
        if not isinstance(entries, (tuple, list)):
            raise ValueError("The current annotation candidate list is invalid.")
        identities.update(entries)
    if any(
        not isinstance(entry, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", entry)
        for entry in identities
    ):
        raise ValueError("The current annotation image identity is invalid.")
    return tuple(sorted(identities))


def _tissue_candidate_scope(target):
    """Allow outline corrections only for the sections in this pending review."""
    result = target.stage_result
    if result.stage != "tissue_candidate":
        return None
    return {"generation_id": result.generation_id,
            "roi_ids": list(result.affected_object_ids)}


def _reference_parent_types(target, reference_labels):
    if target.stage_result.stage != "secondary_classifier_training_plan_review":
        return ["AnalysisDomain"]
    choices = target.stage_result.resume_context["secondary_output_classes_by_candidate_id"]
    matching = [row for row in choices.values()
                if [item["display_name"] for item in row["output_classes"]] == reference_labels]
    if len(matching) != 1:
        raise ValueError("Reopen the selected structure's reference annotations.")
    return [row["parent_id"] for row in matching[0]["eligible_parents"]]


def _primary_reference_parent_ids(store, head, target, entry, annotation_snapshot, reference_labels):
    """Place active pixel references under their exact current material parents."""
    if target.stage_result.stage not in {
        "primary_classifier_training_plan_review", "secondary_classifier_training_plan_review",
    }:
        return None
    from ..accepted_analysis_domain_records import decode_committed_accepted_analysis_domain

    requested = {row.analysis_domain_id: row for row in target.analysis_domain_bindings
                 if row.project_entry_id == entry.project_entry_id}
    parents = {}
    generations = {}
    snapshot = store.reopen_execution_snapshot(expected_head_sha256=head.sha256)
    for authority in snapshot.committed_artifact_authorities:
        if authority.artifact_ref.artifact_kind != "accepted_analysis_domain":
            continue
        domain = decode_committed_accepted_analysis_domain(authority)
        if domain.project_entry_id != entry.project_entry_id:
            continue
        for row in domain.domain_authorities:
            binding = requested.get(row.analysis_domain_id)
            if (binding is not None
                    and row.analysis_domain_revision == binding.analysis_domain_revision
                    and row.analysis_domain_revision_sha256 == binding.analysis_domain_revision_sha256
                    and domain.domain_set.domain_set_sha256 == binding.domain_set_sha256):
                parents[row.analysis_domain_id] = row.source_path_object_id
                generations[row.analysis_domain_id] = domain.tissue_generation_id
    if not requested or parents.keys() != requested.keys():
        raise ValueError("The current reference regions are unavailable. Reopen this review.")
    eligible = _reference_parent_types(target, reference_labels)
    selected = set(parents.values()) if "AnalysisDomain" in eligible else set()
    protected = set(annotation_snapshot["protected_ids"])
    for feature in annotation_snapshot["objects"]["features"]:
        metadata = feature["properties"].get("metadata", {})
        domain_id = metadata.get("analysis_domain_id")
        binding = requested.get(domain_id)
        if (feature["id"] not in protected or binding is None
                or metadata.get("pipeline_active") != "true"
                or metadata.get("pipeline_run_id") != head.batch_id
                or metadata.get("generation_id") != generations[domain_id]
                or metadata.get("analysis_domain_revision_sha256") != binding.analysis_domain_revision_sha256
                or metadata.get("domain_set_sha256") != binding.domain_set_sha256):
            continue
        if ((metadata.get("pipeline_stage") == "primary_classifier"
                and metadata.get("primary_class_id") in eligible)
                or (metadata.get("pipeline_stage") == "secondary_classifier"
                    and metadata.get("candidate_status") == "candidate"
                    and metadata.get("structure_id") in eligible)):
            selected.add(feature["id"])
    if not selected:
        raise ValueError("No accepted parent regions are available for this structure in this image.")
    return sorted(selected)


@contextmanager
def _paused_target(service, batch_id, request_id, expected_head, entry_id=None):
    from ..batch_interactions import project_batch_interaction_target
    from ..batch_state import BatchExecutionState

    with service._provider._use():
        service._require_batch(batch_id)
        store = service._dependencies.runtime.state_store
        with store.acquire_writer_lease("cluster-annotation-review"):
            head = store.load_head()
            if (
                head.sha256 != expected_head
                or head.execution_state is not BatchExecutionState.PAUSED
            ):
                raise ValueError("Refresh the cluster review after processing has paused.")
            if not head.quiescent_pause_sha256 or request_id not in head.pending_interaction_ids:
                raise ValueError("This annotation request is no longer waiting for input.")
            publication = store.reopen_interaction_request_publication(
                request_id, expected_head_sha256=head.sha256
            )
            target = project_batch_interaction_target(publication)
            allowed = annotation_entry_ids(target)
            if entry_id is None and len(allowed) == 1:
                entry_id = allowed[0]
            if entry_id not in allowed:
                raise ValueError("Select one of the images in the current annotation review.")
            catalogue = store.reopen_current_project_catalogue(expected_head_sha256=head.sha256)
            entries = [
                entry
                for entry in catalogue.entries
                if entry.project_entry_id == entry_id and entry.image_role == "primary_mif"
            ]
            if len(entries) != 1:
                raise ValueError("The current primary image is unavailable for this review.")
            yield store, head, target, entries[0]
            if store.load_head().sha256 != expected_head:
                raise ValueError("The cluster analysis changed during annotation review.")


def _validate_removal_owner(attempt, scope, entry_id, image_sha256):
    """Keep the saved removal receipt associated with its actual failed calculation."""
    from mif_pipeline.pipeline_core import StageStatus
    if (attempt.terminal_record_sha256 != scope.get("terminal_record_sha256")
            or attempt.node_id != scope.get("node_id")
            or attempt.stage_result.stage != "measurements"
            or attempt.stage_result.status is not StageStatus.FAILED
            or scope.get("project_entry_id") != entry_id
            or scope.get("image_sha256") != image_sha256
            or attempt.execution_context_binding.current_image_identity_sha256 != image_sha256
            or scope.get("segmentation_artifact_ref_sha256")
            not in attempt.execution_context_binding.input_artifact_ref_sha256s):
        raise ValueError("The cell removal draft differs from its stopped measurement task.")


@contextmanager
def _cell_removal_target(service, batch_id, node_id, expected_head, entry_id=None):
    """Use a current stopped measurement task as the owner of its removal draft."""
    from mif_pipeline.pipeline_core import StageStatus
    with service._provider._use():
        service._require_batch(batch_id)
        store = service._dependencies.runtime.state_store
        with store.acquire_writer_lease("measurement-cell-removals") as lease:
            with service._lock:
                if service._active_wave is not None:
                    raise ValueError("Wait for processing to stop before removing cells.")
            snapshot = store.reopen_execution_snapshot(expected_head_sha256=expected_head)
            head = snapshot.head
            node = next((n for n in snapshot.plan.nodes if n.node_id == node_id), None)
            if (node is None or node.kind != "measurements"
                    or head.node_status_by_id.get(node_id) is not StageStatus.FAILED
                    or head.pending_authorization_sha256_by_node):
                raise ValueError("Open the options for a stopped measurement task to remove cells.")
            attempt_id = head.attempt_ids_by_node[node_id][-1]
            attempt = store.load_attempt(head.terminal_attempt_sha256_by_attempt_id[attempt_id])
            authorities = [a.artifact_ref for a in snapshot.committed_artifact_authorities
                if a.artifact_ref.artifact_kind == "segmentation_result"
                and a.artifact_ref.owning_identity["section_id"] == node.section_target.section_id
                and a.artifact_ref.scope.sample_id == node.participant_ids[0]]
            if len(authorities) != 1:
                raise ValueError("The stopped task has no unique accepted cell population.")
            ref = authorities[0]
            catalogue = store.reopen_current_project_catalogue(expected_head_sha256=head.sha256)
            entry = catalogue.resolve(ref.scope.project_entry_id)
            if entry.image_role != "primary_mif" or entry_id not in (None, entry.project_entry_id):
                raise ValueError("The selected image differs from the stopped measurement task.")
            scope = {key: ref.owning_identity[key] for key in
                ("section_id", "segmentation_population_id", "cell_evidence_sha256")}
            scope.update(node_id=node_id, terminal_record_sha256=attempt.terminal_record_sha256,
                project_entry_id=entry.project_entry_id, image_sha256=entry.image_sha256,
                segmentation_artifact_ref_sha256=ref.artifact_ref_sha256)
            _validate_removal_owner(attempt, scope, entry.project_entry_id, entry.image_sha256)
            source = Path(head.project_path).parent / "data" / entry.project_entry_id / "data.qpdata"
            saved = store.current_project_entry_saved_state_sha256_by_id(
                expected_head_sha256=head.sha256, lease=lease)
            source_sha = _sha(source)
            if source_sha != saved[entry.project_entry_id]:
                raise ValueError("The saved cells changed. Return the owned draft before opening another.")
            yield store, head, scope, entry, source_sha
            if store.load_head().sha256 != expected_head:
                raise ValueError("The analysis changed during cell removal review.")


def export_cell_removals(service, batch_id, node_id, expected_head, entry_id=None):
    """Prepare the existing native draft with one accepted section's removal permission."""
    with _cell_removal_target(service, batch_id, node_id, expected_head, entry_id) as (
            store, head, scope, entry, source_sha):
        folder = _folder(service) / uuid.uuid4().hex
        folder.mkdir(mode=0o700)
        source = Path(head.project_path).parent / "data" / entry.project_entry_id / "data.qpdata"
        snapshot = run_annotation_script(service._dependencies.runtime.runner, folder, {
            "mode": "export", "project_file": head.project_path,
            "project_sha256": _current_project_sha(head.project_path, head.project_qpproj_snapshot_sha256),
            "entry_id": entry.project_entry_id, "cell_removal": scope,
        })
        image = urlparse(entry.canonical_uri)
        if (image.scheme != "file" or image.netloc or image.query or image.fragment
                or snapshot["image_uri"] != entry.canonical_uri
                or snapshot["source_file"] != str(source)
                or snapshot["source_sha256"] != source_sha):
            raise ValueError("The cell removal draft differs from the accepted image.")
        ticket = {"schema_version": "cluster-annotation-ticket/v1", "ticket_id": folder.name,
            "batch_id": batch_id, "request_id": None, "state_head_sha256": head.sha256,
            "interaction_context_sha256": hashlib.sha256(_encoded(scope)).hexdigest(),
            "image_sha256": entry.image_sha256, "image_path": url2pathname(image.path),
            "project_file": head.project_path, **snapshot,
            "project_sha256": head.project_qpproj_snapshot_sha256, "cell_removal": scope}
        _new(folder / "ticket.json", ticket)
        _sync(folder)
        return ticket


def export_annotations(
    service, batch_id, request_id, expected_head, entry_id=None, *, preview_selection=None, reference_role=None
):
    with _paused_target(service, batch_id, request_id, expected_head, entry_id) as (
        store,
        head,
        target,
        entry,
    ):
        folder = _folder(service) / uuid.uuid4().hex
        folder.mkdir(mode=0o700)
        preview = None
        reference = None
        reference_labels = None
        if target.stage_result.stage == "supervised_phenotype_training_plan_review":
            from ..batch_training.supervised_references import reference_choice, reopen_reference_plan

            context = target.stage_result.resume_context
            reopen_reference_plan(context, store=service._dependencies.training.training_plan_set_store)
            reference = reference_choice(context, entry.project_entry_id, reference_role)
            reference_labels = list(reference["class_ids"])
            from ..batch_training.plan_selection import validate_reference_class_names

            validate_reference_class_names(reference_labels)
        elif target.stage_result.stage in {
            "primary_classifier_training_plan_review", "secondary_classifier_training_plan_review",
        }:
            from ..annotation_preview import annotation_reference_labels

            selected = reference_role
            if selected is None and preview_selection is not None:
                selected = {key: preview_selection[key] for key in ("candidate_id", "selection")}
            reference_labels = annotation_reference_labels(
                selected, target.stage_result.resume_context, entry.project_entry_id,
            )
            if preview_selection is not None and any(
                selected[key] != preview_selection[key] for key in ("candidate_id", "selection")
            ):
                raise ValueError("The annotation classes and preview settings differ. Reopen References.")
        elif reference_role is not None:
            raise ValueError("Reference classes are available only in a classifier reference step.")
        if preview_selection is not None:
            if target.stage_result.stage not in {
                "primary_classifier_training_plan_review",
                "secondary_classifier_training_plan_review",
            }:
                raise ValueError(
                    "Annotation preview is available only while collecting classifier references."
                )
            from ..annotation_preview import validate_preview_selection, validate_feedback_roles

            preview = validate_preview_selection(
                preview_selection, target.stage_result.resume_context, entry.project_entry_id
            )
            validate_feedback_roles(
                service._workspace_layout.root,
                preview_selection["selection"],
                target.stage_result.resume_context,
            )
        snapshot = run_annotation_script(
            service._dependencies.runtime.runner,
            folder,
            {
                "mode": "export",
                "project_file": head.project_path,
                "project_sha256": _current_project_sha(
                    head.project_path, head.project_qpproj_snapshot_sha256,
                ),
                "entry_id": entry.project_entry_id,
                "supervised_reference": reference,
            },
        )
        image = urlparse(entry.canonical_uri)
        observed = urlparse(snapshot["image_uri"])
        if (
            image.scheme != "file"
            or image.netloc
            or image.query
            or image.fragment
            or observed.scheme != "file"
            or observed.netloc
            or observed.query
            or observed.fragment
            or Path(url2pathname(observed.path)).as_uri() != Path(url2pathname(image.path)).as_uri()
        ):
            raise ValueError(
                "This annotation workflow requires the current cluster-resident image file."
            )
        expected_source = (
            Path(head.project_path).parent / "data" / entry.project_entry_id / "data.qpdata"
        )
        if (
            Path(snapshot["source_file"]) != expected_source
            or _sha(expected_source) != snapshot["source_sha256"]
        ):
            raise ValueError("The saved image objects differ from this review.")
        ticket = {
            "schema_version": "cluster-annotation-ticket/v1",
            "ticket_id": folder.name,
            "batch_id": batch_id,
            "request_id": request_id,
            "state_head_sha256": head.sha256,
            "interaction_context_sha256": target.interaction_context_sha256,
            "image_sha256": entry.image_sha256,
            "image_path": url2pathname(image.path),
            "project_file": head.project_path,
            **snapshot,
            # The ticket remains bound to the accepted project revision. The native
            # operation separately checks the exact current bytes supplied above.
            "project_sha256": head.project_qpproj_snapshot_sha256,
        }
        if reference is not None:
            ticket["supervised_reference"] = reference
        tissue_candidate = _tissue_candidate_scope(target)
        if tissue_candidate is not None:
            ticket["tissue_candidate"] = tissue_candidate
        if reference_labels is not None:
            ticket["annotation_reference_labels"] = reference_labels
        primary_parents = _primary_reference_parent_ids(
            store, head, target, entry, snapshot["snapshot"], reference_labels,
        )
        if primary_parents is not None:
            ticket["primary_reference_parent_ids"] = primary_parents
        if target.stage_result.stage == "analysis_domain_selection":
            context = target.stage_result.resume_context
            selection_path = context.get("saved_selection_path")
            if not isinstance(selection_path, str) or not Path(selection_path).is_absolute():
                raise ValueError("Open the region selection step before editing analysis regions.")
            ticket["analysis_region_selection"] = {
                "saved_selection_path": selection_path,
                "tissue_generation_id": target.stage_result.generation_id,
            }
        if preview is not None:
            from ..annotation_preview import complete_preview_choice, record_preview_permission

            eligible = (_reference_parent_types(target, reference_labels)
                        if preview["classifier_role"] == "secondary" else None)
            preview = complete_preview_choice(
                preview, snapshot, target.stage_result.resume_context, eligible_parent_ids=eligible
            )
            ticket["annotation_preview"] = preview
            record_preview_permission(service._workspace_layout.root, preview, folder.name)
        _new(folder / "ticket.json", ticket)
        _sync(folder)
        return ticket


def import_annotations(service, batch_id, ticket_id, objects):
    if not isinstance(ticket_id, str) or not re.fullmatch("[0-9a-f]{32}", ticket_id):
        raise ValueError("Select a current annotation review.")
    folder = _folder(service) / ticket_id
    if folder.is_symlink():
        raise ValueError("The annotation review folder was replaced.")
    ticket = _read(folder / "ticket.json")
    if ticket["batch_id"] != batch_id or ticket["ticket_id"] != ticket_id:
        raise ValueError("The annotation review belongs to a different analysis.")
    if ticket.get("annotation_preview") is not None:
        objects = {**objects, "mif_annotation_feedback": True}
    selection = ticket.get("analysis_region_selection")
    if selection is not None and not objects.get("mif_analysis_region_selection"):
        raise ValueError("Select the regions to analyze in QuPath and choose Save selected regions before closing.")
    if selection is None and "mif_analysis_region_selection" in objects:
        raise ValueError("Analysis regions can be selected only from the region selection step.")
    data = _encoded(objects)
    submission_sha = hashlib.sha256(data).hexdigest()
    removal = ticket.get("cell_removal")
    owner = (_cell_removal_target(service, batch_id, removal["node_id"],
                ticket["state_head_sha256"], ticket["entry_id"]) if removal is not None
             else _paused_target(service, batch_id, ticket["request_id"],
                ticket["state_head_sha256"], ticket["entry_id"]))
    with owner as current:
        store, head, target, entry = current[:4]
        context_sha = (hashlib.sha256(_encoded(target)).hexdigest() if removal is not None
                       else target.interaction_context_sha256)
        if (
            context_sha != ticket["interaction_context_sha256"]
            or entry.image_sha256 != ticket["image_sha256"]
            or head.project_qpproj_snapshot_sha256 != ticket["project_sha256"]
        ):
            raise ValueError("The image, labels or model selected for this review have changed.")
        reference = ticket.get("supervised_reference")
        primary_parents = None if removal is not None else _primary_reference_parent_ids(
            store, head, target, entry, ticket["snapshot"], ticket.get("annotation_reference_labels"),
        )
        if ticket.get("primary_reference_parent_ids") != primary_parents:
            raise ValueError("The reference parent regions changed. Open a new annotation review.")
        tissue_candidate = None if removal is not None else _tissue_candidate_scope(target)
        if ticket.get("tissue_candidate") != tissue_candidate:
            raise ValueError("The tissue sections changed. Open a new outline review.")
        if reference is not None:
            from ..batch_training.supervised_references import reference_choice

            expected = reference_choice(target.stage_result.resume_context, entry.project_entry_id, {
                "training_plan_id": reference["training_plan_id"], "split": reference["split"],
            })
            if reference != expected:
                raise ValueError("The reference classes, subject role or cell population changed.")
        source = Path(head.project_path).parent / "data" / entry.project_entry_id / "data.qpdata"
        if str(source) != ticket["source_file"]:
            raise ValueError("The review refers to another saved hierarchy.")
        attempt = folder / submission_sha
        attempt.mkdir(exist_ok=True)
        if attempt.is_symlink():
            raise ValueError("The annotation submission folder was replaced.")
        ready = attempt / "candidate.json"
        candidate = attempt / "candidate.qpdata"
        if ready.exists():
            prepared = _read(ready)
        else:
            if _sha(source) != ticket["source_sha256"]:
                raise ValueError(
                    "The saved annotations changed. Open a new review before returning edits."
                )
            # An interrupted candidate was never published without this ready record.
            candidate.unlink(missing_ok=True)
            prepared = run_annotation_script(
                service._dependencies.runtime.runner,
                attempt,
                {
                    "mode": "candidate",
                    "project_file": head.project_path,
                    "project_sha256": _current_project_sha(
                        head.project_path, ticket["project_sha256"],
                    ),
                    "entry_id": entry.project_entry_id,
                    "source_sha256": ticket["source_sha256"],
                    "snapshot": ticket["snapshot"],
                    "objects": objects,
                    "candidate_file": str(candidate),
                    "supervised_reference": reference,
                    "primary_reference_parent_ids": primary_parents,
                    "annotation_reference_labels": ticket.get("annotation_reference_labels"),
                    "tissue_candidate": tissue_candidate,
                    "cell_removal": removal,
                    "cell_selection": ticket.get("cell_selection"),
                },
            )
            _new(ready, prepared)
            _sync(attempt)
        if (
            prepared.get("source_file") != str(source)
            or prepared.get("candidate_file") != str(candidate)
            or prepared.get("source_sha256") != ticket["source_sha256"]
        ):
            raise ValueError("The native annotation candidate differs from this review.")
        receipt = publish_candidate(
            source, candidate, attempt, ticket["source_sha256"], prepared["candidate_sha256"]
        )
        if selection is not None:
            selected_ids = prepared["changes"].get("selected_region_ids")
            if not selected_ids:
                raise ValueError("The annotation draft did not return any selected analysis regions.")
            destination = Path(selection["saved_selection_path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex)
            try:
                _new(temporary, {
                    "schema_version": "analysis-domain-pathobject-publication/v1",
                    "project_entry_id": entry.project_entry_id,
                    "canonical_image_sha256": entry.image_sha256,
                    "tissue_generation_id": selection["tissue_generation_id"],
                    "candidate_path_object_ids": selected_ids,
                })
                os.replace(temporary, destination)
                _sync(destination.parent)
            finally:
                temporary.unlink(missing_ok=True)
        reference_regions = None
        if removal is None and target.stage_result.stage == "primary_classifier_training_plan_review":
            from ..annotation_region_summary import saved_area_region_counts

            counts = saved_area_region_counts(objects, ticket["snapshot"]["protected_ids"])
            if counts is not None:
                reference_regions = {
                    "request_id": ticket["request_id"],
                    "interaction_context_sha256": ticket["interaction_context_sha256"],
                    "project_entry_id": ticket["entry_id"],
                    "saved_annotations_sha256": receipt["accepted_sha256"],
                    **counts,
                }
        return {
            **({"reference_regions": reference_regions} if reference_regions is not None else {}),
            "status": "annotations_saved",
            "ticket_id": ticket_id,
            "changes": prepared["changes"],
            "state_head_sha256": head.sha256,
            "saved_annotations_sha256": receipt["accepted_sha256"],
        }


def prepare_local_project(runner, folder, mounted_image, ticket, *, mounted_images=None):
    """Build a separate local project containing annotation shapes and streamed pixels."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    local = folder / ("review-" + uuid.uuid4().hex)
    local.mkdir()
    from mif_pipeline.io.aligned_collection import is_aligned_source_collection
    if mounted_images is not None and is_aligned_source_collection(mounted_image):
        from .mounted_source_collection import prepare_mounted_collection

        mounted_image, binding = prepare_mounted_collection(
            source=mounted_image, remote_path=ticket["image_path"],
            expected_sha256=ticket["image_sha256"], mounted=mounted_images,
            directory=local / "source")
        _new(local / "source-collection-binding.json", binding)
    classes = list(ticket["class_names"])
    active = ticket.get("annotation_reference_labels")
    if active is not None:
        used = {
            feature.get("properties", {}).get("classification", {}).get("name")
            for feature in ticket["snapshot"]["objects"]["features"]
            if isinstance(feature.get("properties", {}).get("classification"), dict)
        }
        folded = {name.casefold() for name in active}
        # Hide unused stale palette aliases in this draft; never rename saved objects.
        classes = list(dict.fromkeys([*active, *(
            name for name in classes
            if name in used or name.casefold() not in folded
        )]))
    reference = ticket.get("supervised_reference")
    result = run_annotation_script(
        runner,
        local,
        {
            "mode": "local",
            "local_project_folder": str(local / "project"),
            "local_image": str(mounted_image),
            "snapshot": ticket["snapshot"],
            "image_name": ticket["image_name"],
            "image_type": ticket["image_type"],
            "class_names": classes,
            "image_signature": ticket["image_signature"],
            "supervised_reference": reference,
            "tissue_candidate": ticket.get("tissue_candidate"),
            "cell_selection": ticket.get("cell_selection"),
            "cell_removal": ticket.get("cell_removal"),
            "cell_correction": ticket.get("cell_correction"),
            "correction_outline": ticket.get("correction_outline"),
        },
    )
    _new(local / "cluster-ticket.json", {**ticket, "local_image_path": str(mounted_image)})
    project = Path(result["project_file"])
    _materialize_local_project(project)
    if ticket.get("annotation_preview") is not None:
        _new(
            project.parent / "annotation-preview.json",
            {**ticket["annotation_preview"], "ticket_id": ticket["ticket_id"]},
        )
    if ticket.get("analysis_region_selection") is not None:
        _new(project.parent / "analysis-region-selection.json", {
            "ticket_id": ticket["ticket_id"], "entry_id": result["entry_id"],
        })
    return project


def _materialize_local_project(project):
    from mif_pipeline.qupath_runtime.project import ProjectLayout
    from mif_pipeline.qupath_runtime.scripts import materialize_script_bundle, registered_script_assets

    base = project.parent
    layout = ProjectLayout(
        base,
        project,
        base / "scripts",
        base / "classifiers/pixel_classifiers",
        base / "classifiers/object_classifiers",
        base / "data",
        base / "exchange",
        base / "checkpoints",
        base / "provenance",
        base / "runs",
        base / "data/config.json",
        "{}",
    )
    materialize_script_bundle(layout, registered_script_assets())


def prepare_local_correction_project(runner, folder, tickets, *, image_names=None):
    """Keep one entry per existing correction ticket in the supplied image order."""
    tickets = list(tickets)
    if (not tickets or len({ticket["ticket_id"] for ticket in tickets}) != len(tickets)
            or len({(ticket["batch_id"], ticket["state_head_sha256"]) for ticket in tickets}) != 1
            or any(not ticket.get("cell_correction", {}).get("operations") for ticket in tickets)):
        raise ValueError("Reopen the collected corrections for the current images.")
    local = Path(folder) / ("review-" + uuid.uuid4().hex)
    local.mkdir(parents=True)
    fields = ("snapshot", "image_name", "image_type", "class_names", "image_signature",
              "cell_selection", "cell_correction", "correction_outline")
    names = [ticket["image_name"] for ticket in tickets] if image_names is None else list(image_names)
    if len(names) != len(tickets) or any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("Name each image in the collected corrections.")
    labels = [name if names.count(name) == 1 else f"{name} · area {names[:index].count(name) + 1}"
              for index, name in enumerate(names)]
    result = run_annotation_script(runner, local, {
        "mode": "local-corrections", "local_project_folder": str(local / "project"),
        "images": [{**{key: ticket[key] for key in fields}, "local_image": ticket["image_path"], "image_name": label}
                   for ticket, label in zip(tickets, labels)],
    })
    entries = result["entries"]
    if len(entries) != len(tickets) or len({entry["entry_id"] for entry in entries}) != len(tickets):
        raise ValueError("The correction project did not retain every selected image.")
    _new(local / "correction-tickets.json", {
        "schema_version": "local-cell-corrections/v1",
        "images": [{"entry_id": entry["entry_id"], "display_name": label,
                    "ticket": {**ticket, "local_image_path": ticket["image_path"]}}
                   for entry, ticket, label in zip(entries, tickets, labels)],
    })
    project = Path(result["project_file"])
    _materialize_local_project(project)
    return project


def _check_local_image(result, ticket):
    uri = urlparse(result.get("image_uri", ""))
    if (uri.scheme != "file" or uri.netloc or uri.query or uri.fragment
            or Path(url2pathname(uri.path)).as_uri() != Path(ticket["local_image_path"]).as_uri()
            or result.get("image_signature") != ticket["image_signature"]):
        raise ValueError("The local annotation project now refers to a different image or spatial calibration.")


def collect_local_corrections(runner, project_file):
    """Return each mapped image with its own unchanged ticket and candidate objects."""
    project = Path(project_file)
    manifest = _read(project.parent.parent / "correction-tickets.json")
    if manifest.get("schema_version") != "local-cell-corrections/v1":
        raise ValueError("Reopen the collected corrections from the application.")
    images = manifest["images"]
    result = run_annotation_script(runner, project.parent.parent, {
        "mode": "collect-local-corrections", "project_file": str(project),
        "project_sha256": _sha(project), "entry_ids": [image["entry_id"] for image in images],
    })
    entries = result["entries"]
    if [entry["entry_id"] for entry in entries] != [image["entry_id"] for image in images]:
        raise ValueError("The returned corrections refer to different images.")
    returned = []
    for image, entry in zip(images, entries):
        _check_local_image(entry, image["ticket"])
        returned.append((image["ticket"], entry["objects"]))
    return returned


def collect_local_annotations(runner, project_file):
    project = Path(project_file)
    ticket = _read(project.parent.parent / "cluster-ticket.json")
    result = run_annotation_script(
        runner,
        project.parent.parent,
        {
            "mode": "collect-local",
            "project_file": str(project),
            "project_sha256": _sha(project),
            "entry_id": "1",
            "cell_removal": ticket.get("cell_removal"),
            "cell_correction": ticket.get("cell_correction"),
        },
    )
    _check_local_image(result, ticket)
    return ticket, result["objects"]


def stage_annotations(service, batch_id, value):
    """Retain a bounded draft; this does not accept or alter any annotations."""
    from collections.abc import Mapping

    if not isinstance(value, Mapping) or set(value) != {"ticket_id", "objects"}:
        raise ValueError("Return a current annotation review and its edited objects.")
    ticket_id = value["ticket_id"]
    if not isinstance(ticket_id, str) or not re.fullmatch("[0-9a-f]{32}", ticket_id):
        raise ValueError("Select a current annotation review.")
    objects = value["objects"]
    if (
        not isinstance(objects, Mapping)
        or objects.get("type") != "FeatureCollection"
        or not isinstance(objects.get("features"), list)
    ):
        raise ValueError("Return annotation regions as a GeoJSON feature collection.")
    with service._provider._use():
        service._require_batch(batch_id)
        folder = _folder(service)
        ticket = _read(folder / ticket_id / "ticket.json")
        if ticket.get("batch_id") != batch_id or ticket.get("ticket_id") != ticket_id:
            raise ValueError("The annotation draft belongs to another analysis.")
        submission_id = uuid.uuid4().hex
        _new(folder / (submission_id + ".submission.json"), dict(value))
        _sync(folder)
        return {"status": "draft_received", "submission_id": submission_id}


def import_staged_annotations(service, batch_id, submission_id):
    if not isinstance(submission_id, str) or not re.fullmatch("[0-9a-f]{32}", submission_id):
        raise ValueError("Select the submitted annotation draft.")
    value = _read(_folder(service) / (submission_id + ".submission.json"))
    return import_annotations(service, batch_id, value["ticket_id"], value["objects"])


def parse_submission(text):
    if not isinstance(text, str) or len(text.encode()) > MAX_JSON:
        raise ValueError("The annotation submission exceeds 32 MiB.")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Repeated annotation field.")
            result[key] = value
        return result

    value = json.loads(
        text,
        object_pairs_hook=unique,
        parse_constant=lambda _v: (_ for _ in ()).throw(
            ValueError("Nonfinite annotation coordinate.")
        ),
    )
    if not isinstance(value, dict) or set(value) != {"ticket_id", "objects"}:
        raise ValueError("Return the current ticket and its edited annotation regions.")
    return value
