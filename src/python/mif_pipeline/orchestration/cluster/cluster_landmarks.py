"""Prepare manual alignment through local editing and cluster-owned fitting.

We export the selected image pair for mounted-image viewing. The first saved
edit supplies the starting pose and image-based affine settings. Its cluster
fit returns to the same local project for global review and optional local
landmark refinement. Only the final affine or TPS candidate enters the analysis
review; the intermediate affine remains inside its editing session.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from contextlib import contextmanager
from pathlib import Path
import hashlib, json, os, re, uuid
from ..alignment_context import current_alignment_context
from ..batch_state import BatchExecutionState
from .cluster_annotations import _read
from .cluster_annotations import _new
from .cluster_annotations import _sha
from .cluster_annotations import _sync
from .cluster_annotations import _encoded
from .cluster_landmark_exchange import run_native
from .cluster_landmark_exchange import ticket_digest
from .cluster_landmark_exchange import validate_ticket
from mif_pipeline.registration.landmarks import validate_manual_alignment

# ==============================================================================
#                               Cluster landmarks
# ==============================================================================


def _identifier(value):
    """

    Checks the identifier used to find a saved landmark request.

    Args:
        value: Thirty-two-character lowercase hexadecimal request identifier.

    Returns:
        Checked identifier for constructing a filename inside the landmark folder.

    Raises:
        ValueError: The identifier has an unsupported type or format.
    """

    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{32}", value):
        raise ValueError("Choose a current landmark session.")
    return value


def _folder(service):
    """

    Prepares the landmark-review folder inside the current cluster analysis.

    Args:
        service: Analysis service whose workspace supplies the parent directory.

    Returns:
        Path to cluster-reviews/landmarks, created when needed.

    Raises:
        ValueError: The landmark directory is a symbolic link.
        OSError: Its parent directories cannot be created.
    """

    folder = service._workspace_layout.root / "cluster-reviews" / "landmarks"
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink():
        raise ValueError("The landmark folder is linked.")
    return folder


def _request(value):
    """

    Decodes a saved alignment response through the current analysis-response reader.

    Args:
        value: JSON-compatible response retaining the selected review, target
            and expected analysis state.

    Returns:
        Typed response used to resolve the current image pair.
    """

    from ..batch_cli import _response_text

    return _response_text(json.dumps(value, allow_nan=False))


def _source_facts(snapshot):
    """

    Checks the two selected cluster files and records their current filesystem values.

    Each image retains its source URI while the image owner identifies its
    regular identity member. Vendor images use the source file; completed
    OME-Zarr images use root metadata. We compare that member's device/file ID,
    size and timestamps with the selected source and the saved session. This
    detects replacement while reusing the completed image identity.

    Args:
        snapshot: Recorded registration image pair with original source URIs
            and the file identities obtained when those images were inspected.

    Returns:
        Fixed and moving dictionaries containing device, file ID, size,
        modification time and metadata-change time.

    Raises:
        ValueError: A selected source is not a cluster file or differs from
            the file identified in the image-pair snapshot.
    """

    from urllib.parse import urlsplit, unquote
    import stat

    rows = {}
    for role in ("fixed", "moving"):
        entry = getattr(snapshot, role + "_entry")
        uri = urlsplit(entry.source_uri)
        if uri.scheme != "file" or uri.netloc or uri.query or uri.fragment:
            raise ValueError("Landmark alignment requires cluster-resident source images.")
        path = Path(unquote(uri.path))
        from mif_pipeline.io import image_identity_member

        current = image_identity_member(path).lstat()
        expected = entry.source_file_identity.platform_identity
        if (
            not stat.S_ISREG(current.st_mode)
            or not hasattr(expected, "device")
            or (current.st_dev, current.st_ino) != (expected.device, expected.inode)
        ):
            raise ValueError("A source image differs from the image selected for registration.")
        rows[role] = {
            "device": current.st_dev,
            "inode": current.st_ino,
            "size": current.st_size,
            "modified_ns": current.st_mtime_ns,
            "changed_ns": current.st_ctime_ns,
        }
    return rows


@contextmanager
def _review(service, batch_id, request):
    """

    Keeps one paused image-pair review stable while its landmark operation is performed.

    We require processing to be paused, resolve the selected alignment step and
    obtain the analysis's exclusive write permission. This prevents another
    pipeline update from replacing the selected image pair during export or fit.
    After the operation, the source-file values and saved analysis state must
    still agree with those observed when it began.

    Args:
        service: Cluster analysis service containing the current state and runner.
        batch_id: Analysis to which the landmark review belongs.
        request: Current alignment response identifying the selected image pair.

    Yields:
        Analysis-state store and the resolved alignment context for use inside
        a with block. Locks and the service connection are released on exit.

    Raises:
        ValueError: Processing is active, the pause is incomplete, or the source
            images or review change during the operation.
    """

    with service._provider._use():
        service._require_batch(batch_id)
        with service._lock:
            if service._active_wave is not None:
                raise ValueError("Wait for processing to pause before opening landmarks.")
            store = service._dependencies.runtime.state_store
            with store.acquire_writer_lease("cluster-landmark-review"):
                context = current_alignment_context(store, request)
                head = context[0]
                if (
                    head.execution_state is not BatchExecutionState.PAUSED
                    or not head.quiescent_pause_sha256
                ):
                    raise ValueError("Refresh the alignment review after processing has paused.")
                sources = _source_facts(context[1])
                yield store, context
                if _source_facts(context[1]) != sources:
                    raise ValueError("A source image changed during landmark processing.")
                if store.load_head().sha256 != head.sha256:
                    raise ValueError("The alignment review changed. Refresh it before continuing.")


def _entries(snapshot):
    """

    Collects the image descriptions required by the native landmark calculation.

    We include each project's saved image-data checksum so QuPath can verify the
    expected tissue and image information before reading it. The X/Y pixel sizes
    are decoded from their recorded hexadecimal float strings into µm/pixel.

    Args:
        snapshot: Current registration image-pair description.

    Returns:
        Fixed and moving dictionaries containing entry IDs, source URIs,
        full-resolution dimensions, calibration and image-data checksums.

    Raises:
        ValueError: Saved image information is absent for either selected entry.
    """

    rows = {}
    for role in ("fixed", "moving"):
        entry = getattr(snapshot, role + "_entry")
        member = next(row for row in entry.members if row.target_kind == "entry_qpdata")
        if member.presence != "present":
            raise ValueError("The saved image information is unavailable.")
        rows[role] = {
            "project_entry_id": entry.project_entry_id,
            "source_uri": entry.source_uri,
            "width": entry.level0_size_x_px,
            "height": entry.level0_size_y_px,
            "x_um": float.fromhex(entry.x_um_per_px_hex),
            "y_um": float.fromhex(entry.y_um_per_px_hex),
            "qpdata_sha256": member.content_sha256,
        }
    return rows


def stage_review(service, batch_id, text):
    """

    Saves the request for a new cluster landmark export.

    We first check that the response identifies a current paused alignment
    review. Its JSON is then written under a newly generated request identifier
    and the folder is synchronized before returning that identifier. The
    subsequent export job reads this saved request.

    Args:
        service: Cluster-side analysis service.
        batch_id: Selected analysis ID.
        text: Current alignment response encoded as JSON, limited to 1 MiB.

    Returns:
        Dictionary containing the submission ID for the export job.

    Raises:
        ValueError: The response is invalid, oversized or no longer current.
    """

    if not isinstance(text, str) or len(text.encode()) > 1024 * 1024:
        raise ValueError("The landmark request is too large.")
    from ..batch_cli import _response_text

    request = _response_text(text)
    with _review(service, batch_id, request):
        identifier = uuid.uuid4().hex
        _new(_folder(service) / (identifier + ".request.json"), json.loads(text))
        _sync(_folder(service))
        return {"submission_id": identifier}


def export_review(service, batch_id, identifier):
    """

    Exports the selected image readers and accepted tissue outlines for local landmark editing.

    From the saved request, we resolve the fixed mIF entry, moving paired entry
    and accepted tissue annotations. The native export reads these entries and
    returns their source-reader descriptions, calibration and tissue GeoJSON.
    We combine that result with the current analysis review and source-file facts,
    then save one checksum covering the complete exported description.

    Repeating an export with the same request and image-pair snapshot reuses its
    saved description. A transform already present for this pair must be reviewed
    before another new landmark proposal is started.

    Args:
        service: Cluster analysis service with its native QuPath runner.
        batch_id: Analysis whose alignment step is being exported.
        identifier: Submission ID returned by stage_review().

    Returns:
        Saved cluster-landmark-session/v1 description used to create the local
        viewing project and check the later returned point pairs.

    Raises:
        ValueError: The request or image pair changed, an alignment already
            exists, or the native export is incomplete.
    """

    identifier = _identifier(identifier)
    folder = _folder(service)
    encoded = _read(folder / (identifier + ".request.json"))
    request = _request(encoded)
    with _review(service, batch_id, request) as (store, context):
        _, snapshot, project, destination, tissue_ids, _ = context
        if destination.exists():
            raise ValueError("This pair already has an alignment. Refresh its review.")
        session = folder / identifier
        session.mkdir(mode=0o700, exist_ok=True)
        ticket_path = session / "ticket.json"
        if ticket_path.exists():
            ticket = validate_ticket(_read(ticket_path))
            if (
                ticket["request"] != encoded
                or ticket["snapshot_sha256"] != snapshot.snapshot_resolution_sha256
            ):
                raise ValueError("The saved landmark session differs from this review.")
            return ticket
        result, _ = run_native(
            service._dependencies.runtime.runner,
            session,
            {
                "mode": "export",
                "project_file": str(project),
                "project_sha256": snapshot.qpproj_sha256,
                "entries": _entries(snapshot),
                "tissue_object_ids": tissue_ids,
            },
        )
        if set(result) != {"pair", "tissue_objects"}:
            raise ValueError("The landmark image pair is incomplete.")
        ticket = {
            "schema": "cluster-landmark-session/v1",
            "ticket_id": identifier,
            "batch_id": batch_id,
            "request": encoded,
            "snapshot_sha256": snapshot.snapshot_resolution_sha256,
            "source_files": _source_facts(snapshot),
            **result,
        }
        ticket["ticket_sha256"] = ticket_digest(ticket)
        validate_ticket(ticket)
        _new(ticket_path, ticket)
        _sync(session)
        return ticket


def stage_landmarks(service, batch_id, text):
    """

    Validates and saves the current manual-alignment step from local QuPath.

    We compare the returned review ID and checksum with the exported image pair,
    then validate either its starting pose and affine settings or its local
    refinement choice and point coordinates. Before saving the submission, the cluster review must
    still contain the same image-pair snapshot and source-file values.

    Args:
        service: Cluster-side analysis service.
        batch_id: Analysis receiving the completed landmark edit.
        text: JSON containing ticket_id, ticket_sha256 and the alignment
            phase request, limited to 1 MiB.

    Returns:
        Submission ID used by the landmark fitting job.

    Raises:
        ValueError: The return belongs to another review, point values are invalid,
            or the source images or analysis changed while the edit was open.
    """

    if not isinstance(text, str) or len(text.encode()) > 1024 * 1024:
        raise ValueError("The landmark submission is too large.")

    def unique(pairs):
        """

        Checks each returned JSON object for duplicate field names.

        Args:
            pairs: Ordered fields supplied by the JSON decoder.

        Returns:
            Dictionary containing the submitted values.

        Raises:
            ValueError: A repeated field would overwrite an earlier landmark value.
        """

        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Repeated landmark submission field.")
            result[key] = value
        return result

    value = json.loads(
        text,
        object_pairs_hook=unique,
        parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("Nonfinite landmark value.")),
    )
    if not isinstance(value, dict) or set(value) != {"ticket_id", "ticket_sha256", "alignment"}:
        raise ValueError("Return the saved manual alignment for this review.")
    ticket = validate_ticket(
        _read(_folder(service) / _identifier(value["ticket_id"]) / "ticket.json")
    )
    if ticket["batch_id"] != batch_id or ticket["ticket_sha256"] != value["ticket_sha256"]:
        raise ValueError("These landmarks belong to another analysis or image pair.")
    pairs = ticket["pair"]
    sizes = {
        role: (row["signature"]["width"], row["signature"]["height"]) for role, row in pairs.items()
    }
    value["alignment"] = validate_manual_alignment(
        value["alignment"], fixed_size=sizes["fixed"], moving_size=sizes["moving"]
    )
    with _review(service, batch_id, _request(ticket["request"])) as (_, context):
        if (
            context[1].snapshot_resolution_sha256 != ticket["snapshot_sha256"]
            or _source_facts(context[1]) != ticket["source_files"]
        ):
            raise ValueError("The image pair changed while its landmarks were being edited.")
        identifier = uuid.uuid4().hex
        _new(_folder(service) / (identifier + ".alignment.json"), value)
        _sync(_folder(service))
        return {"submission_id": identifier}


def alignment_publication_directory(destination):
    """Keep fitting descriptions outside the image entry's saved hierarchy.

    The admitted conventional destination is project/data/fixed/transform.json.
    Pair-specific proposal files belong beside data, so publishing or retaining
    a fit cannot change the saved image hierarchy observed by registration.
    """
    destination = Path(destination)
    folder = destination.parent.parent.parent / "registration" / destination.stem
    if folder.is_symlink() or folder.parent.is_symlink():
        raise ValueError("The alignment description directory is linked. Restore its original folder.")
    return folder


def publish_candidate(destination, candidate, receipt, *, record, candidate_sha256,
                      predecessor_sha256):
    """Publish one reviewed image-pair proposal and retain a replaced transform.

    Manual and automatic fitting use their existing receipt and candidate hash.
    A replacement must still match the transform in the admitted review snapshot.
    We retain its bytes and fitting receipts before replacing the current files;
    publication failure restores the exact prior files. An identical completed
    publication can be reused without fitting again. The caller holds the
    analysis writer lease and checks the current review and image pair.
    """
    candidate, destination, record = Path(candidate), Path(destination), Path(record)
    if (candidate.is_symlink() or not candidate.is_file()
            or candidate.stat().st_size > 16 * 1024 * 1024):
        raise ValueError("The alignment candidate is unavailable or changed before publication.")
    candidate_data = candidate.read_bytes()
    if (len(candidate_data) > 16 * 1024 * 1024
            or hashlib.sha256(candidate_data).hexdigest() != candidate_sha256):
        raise ValueError("The alignment candidate is unavailable or changed before publication.")
    if (record.parent != alignment_publication_directory(destination) or destination.is_symlink()
            or record.is_symlink()):
        raise ValueError("The saved alignment destination changed. Reopen alignment setup.")
    record.parent.mkdir(parents=True, exist_ok=True)
    payload = _encoded(receipt)
    if destination.exists() and destination.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("The saved alignment is too large. Restore its original file.")
    predecessor_data = destination.read_bytes() if destination.exists() else None
    if predecessor_data is not None and len(predecessor_data) > 16 * 1024 * 1024:
        raise ValueError("The saved alignment is too large. Restore its original file.")
    current_sha = hashlib.sha256(predecessor_data).hexdigest() if predecessor_data is not None else None
    if (current_sha == candidate_sha256 and record.is_file()
            and _read(record) == receipt):
        return
    if current_sha != predecessor_sha256:
        raise ValueError("The saved transform changed since this alignment review. Reopen alignment setup.")
    sidecars = (record.parent / "automatic.json", record.parent / "landmark-candidate.json")
    previous = {destination: predecessor_data}
    for path in sidecars:
        if path.is_symlink() or (path.exists() and path.stat().st_size > 16 * 1024 * 1024):
            raise ValueError("The saved alignment description is linked or too large. Restore its original file.")
        previous[path] = path.read_bytes() if path.exists() else None
    if record not in previous:
        raise ValueError("The alignment description does not belong to this image pair.")
    if current_sha is None and previous[record] is not None and _read(record) != receipt:
        raise ValueError("A different alignment proposal is already recorded. Reopen alignment setup.")

    def stage(path, data):
        staged = path.with_name(".alignment-" + uuid.uuid4().hex + ".json")
        try:
            with staged.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        return staged

    if current_sha is not None:
        history = record.parent / "history" / receipt["recipe_sha256"]
        if history.is_symlink() or history.parent.is_symlink():
            raise ValueError("The alignment history directory is linked.")
        history.mkdir(parents=True, exist_ok=True)
        for path, data in previous.items():
            if data is None:
                continue
            retained = history / path.name
            if retained.exists():
                if retained.is_symlink() or retained.read_bytes() != data:
                    raise ValueError("The retained alignment history changed.")
            else:
                staged = stage(retained, data)
                try:
                    os.link(staged, retained)
                finally:
                    staged.unlink(missing_ok=True)
        _sync(history)
        _sync(history.parent)

    staged_transform, staged_record = None, None
    changed = []
    try:
        staged_transform = stage(destination, candidate_data)
        staged_record = stage(record, payload)
        if any(path.is_symlink() or (path.read_bytes() if path.exists() else None) != data
               for path, data in previous.items()):
            raise ValueError("The saved alignment changed before publication. Reopen alignment setup.")
        if current_sha is None:
            if previous[record] is None:
                os.link(staged_record, record)
                changed.append(record)
            os.link(staged_transform, destination)
            changed.append(destination)
        else:
            os.replace(staged_record, record)
            changed.append(record)
            os.replace(staged_transform, destination)
            changed.append(destination)
            for path in sidecars:
                if path != record and previous[path] is not None:
                    path.unlink()
                    changed.append(path)
        _sync(destination.parent)
        _sync(record.parent)
        _sync(record.parent.parent)
    except BaseException as error:
        for path in reversed(changed):
            try:
                data = previous[path]
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    restored = stage(path, data)
                    try:
                        os.replace(restored, path)
                    finally:
                        restored.unlink(missing_ok=True)
            except BaseException as rollback_error:
                error.add_note(f"Alignment rollback failed for {path}: {rollback_error}")
        if changed:
            try:
                _sync(destination.parent)
                _sync(record.parent)
            except BaseException as rollback_error:
                error.add_note(f"Alignment rollback synchronization failed: {rollback_error}")
        raise
    finally:
        for staged in (staged_transform, staged_record):
            if staged is not None:
                staged.unlink(missing_ok=True)


def _fit_recipe(ticket, alignment):
    """Bind a fitting request to its image pair and the native calculations we run.

    We retain helper identities in the recipe so an edited fitting implementation
    cannot silently reuse an earlier result. The returned digest names one small
    session record; images and transforms remain in their existing locations.
    """
    from mif_pipeline.qupath_runtime.scripts import (
        QuPathScriptAsset as Asset, registered_script_asset_sha256,
    )
    helpers = {
        name: registered_script_asset_sha256(asset)
        for name, asset in (("geometry", Asset.PAIRED_GEOMETRY),
                            ("algorithm", Asset.AUTOMATIC_ELASTIX),
                            ("exchange", Asset.LANDMARK_EXCHANGE))
    }
    return hashlib.sha256(_encoded({
        "ticket": ticket["ticket_sha256"], "alignment": alignment, "helpers": helpers,
    })).hexdigest()


def _check_global_result(result, ticket, alignment, fit_id, session):
    """Reopen the session's affine result and its exact completed transform.

    The cluster supplies the transform location. We require the same exported
    image pair, normalized starting settings and fitting recipe, then check the
    completed file before returning its path. Local submissions supply its ID
    and checksum rather than a replacement source path.
    """
    fields = {"schema", "ticket_sha256", "global_fit_id", "global_transform_path",
              "global_transform_sha256", "alignment", "global_metrics",
              "fixed_entry_id", "moving_entry_id", "direction"}
    if (not isinstance(result, dict) or set(result) != fields
            or result["schema"] != "manual-global-result/v1"
            or result["ticket_sha256"] != ticket["ticket_sha256"]
            or result["global_fit_id"] != fit_id
            or result["alignment"] != alignment
            or result["direction"] != "moving-to-fixed-level0"
            or any(result[role + "_entry_id"] != ticket["pair"][role]["project_entry_id"]
                   for role in ("fixed", "moving"))
            or not isinstance(result["global_metrics"], dict)
            or not isinstance(result["global_transform_path"], str)):
        raise ValueError("The global affine does not match this image pair and starting position.")
    path = Path(result["global_transform_path"])
    if (not path.is_absolute() or ".." in path.parts or not path.is_relative_to(session)
            or path.is_symlink() or not path.is_file()
            or path.stat().st_size > 16 * 1024 * 1024
            or _sha(path) != result["global_transform_sha256"]):
        raise ValueError("The saved global affine changed. Fit the starting position again.")
    return path


def import_landmarks(service, batch_id, identifier):
    """Fit the saved manual-alignment phase on the original cluster images.

    We first fit a global affine from the selected image channels and optional
    starting pose. That result stays in the editing session and is returned for
    local overlay review. A subsequent final request identifies this affine and
    supplies either its acceptance for final review or the local TPS point pairs.
    Only the final request publishes a candidate and advances the interaction.

    Args:
        service: Cluster analysis service with its native runner.
        batch_id: Analysis owning the exported image pair.
        identifier: Saved submission ID returned by stage_landmarks().

    Returns:
        The intermediate global result, or the ordinary final review response.

    Raises:
        ValueError: The image pair, request, saved transform or analysis changed.
        OSError: A fitting result or its completed candidate cannot be saved.
    """
    value = _read(_folder(service) / (_identifier(identifier) + ".alignment.json"))
    session = _folder(service) / _identifier(value["ticket_id"])
    ticket = validate_ticket(_read(session / "ticket.json"))
    request = _request(ticket["request"])
    if ticket["batch_id"] != batch_id or ticket["ticket_sha256"] != value["ticket_sha256"]:
        raise ValueError("This manual alignment belongs to another image pair.")
    with _review(service, batch_id, request) as (store, context):
        head, snapshot, project, destination, tissue_ids, _ = context
        if (snapshot.snapshot_resolution_sha256 != ticket["snapshot_sha256"]
                or _source_facts(snapshot) != ticket["source_files"]):
            raise ValueError("The alignment images changed.")
        entries = _entries(snapshot)
        alignment = validate_manual_alignment(
            value["alignment"],
            fixed_size=(entries["fixed"]["width"], entries["fixed"]["height"]),
            moving_size=(entries["moving"]["width"], entries["moving"]["height"]),
        )
        common = {
            "project_file": str(project), "project_sha256": snapshot.qpproj_sha256,
            "entries": entries, "tissue_object_ids": tissue_ids,
            "alignment": alignment, "ticket_sha256": ticket["ticket_sha256"],
        }
        recipe = _fit_recipe(ticket, alignment)
        # i) Keep G as an intermediate session result while its overlay is reviewed.
        if alignment["stage"] == "global":
            fitted = session / ("global-" + recipe + ".json")
            if fitted.exists():
                result = _read(fitted)
                _check_global_result(result, ticket, alignment, recipe, session)
            else:
                if (destination.is_symlink() or (_sha(destination) if destination.exists() else None)
                        != snapshot.transform.content_sha256):
                    raise ValueError("The saved alignment changed. Reopen alignment setup.")
                from mif_pipeline.registration.automatic import find_elastix_directory
                result, operation = run_native(
                    service._dependencies.runtime.runner, session,
                    {**common, "mode": "fit-global", "global_fit_id": recipe,
                     "tools_directory": str(find_elastix_directory(alignment["tools_directory"]))},
                )
                if result.get("global_transform_path") != str(operation / "global.json"):
                    raise ValueError("The global affine has an unexpected result location.")
                _check_global_result(result, ticket, alignment, recipe, session)
                _new(fitted, result)
                _sync(session)
            return result

        # ii) Resolve the reviewed G on the cluster; the returned edit supplies no path.
        global_id = alignment["global_fit_id"]
        global_result = _read(session / ("global-" + global_id + ".json"))
        global_alignment = validate_manual_alignment(
            global_result.get("alignment"),
            fixed_size=(entries["fixed"]["width"], entries["fixed"]["height"]),
            moving_size=(entries["moving"]["width"], entries["moving"]["height"]),
        )
        if (global_alignment["stage"] != "global"
                or _fit_recipe(ticket, global_alignment) != global_id
                or global_result.get("global_transform_sha256") != alignment["global_transform_sha256"]):
            raise ValueError("The final request differs from its reviewed global affine.")
        global_path = _check_global_result(global_result, ticket, global_alignment, global_id, session)
        fitted = session / ("final-" + recipe + ".json")
        retained = fitted.exists()
        if retained:
            result = _read(fitted)
        else:
            if (destination.is_symlink() or (_sha(destination) if destination.exists() else None)
                    != snapshot.transform.content_sha256):
                raise ValueError("The saved alignment changed. Reopen alignment setup.")
            result, operation = run_native(
                service._dependencies.runtime.runner, session,
                {**common, "mode": "fit-final", "global_transform_path": str(global_path)},
            )
            if result.get("candidate_path") != str(operation / "candidate.json"):
                raise ValueError("The final alignment has an unexpected result location.")
        expected_fields = {"schema", "ticket_sha256", "alignment", "method", "qc",
                           "local_rms_um", "fixed_entry_id", "moving_entry_id", "direction",
                           "candidate_path", "candidate_sha256"}
        expected_method = "global-affine-local-tps" if alignment["local_refinement"] else "global-affine"
        if (set(result) != expected_fields or result["schema"] != "manual-final-result/v1"
                or result["method"] != expected_method
                or result["alignment"] != alignment
                or result["fixed_entry_id"] != entries["fixed"]["project_entry_id"]
                or result["moving_entry_id"] != entries["moving"]["project_entry_id"]
                or result["ticket_sha256"] != ticket["ticket_sha256"]
                or result["direction"] != "moving-to-fixed-level0"):
            raise ValueError("The final alignment does not match its reviewed affine and local choice.")
        candidate = Path(result["candidate_path"])
        if (not candidate.is_absolute() or ".." in candidate.parts
                or not candidate.is_relative_to(session)):
            raise ValueError("The final alignment does not match its reviewed affine and local choice.")
        if not retained:
            _new(fitted, result)
            _sync(session)
        if (store.load_head().sha256 != head.sha256
                or _source_facts(snapshot) != ticket["source_files"]):
            raise ValueError("The alignment review or images changed before publication.")
        # iii) Publication is shared by affine-only and locally refined final maps.
        publish_candidate(destination, candidate, {
            "schema": "landmark-candidate/v1", "recipe_sha256": recipe,
            "ticket_sha256": ticket["ticket_sha256"],
            "candidate_sha256": result["candidate_sha256"],
            "expected_head_sha256": head.sha256,
            "snapshot_sha256": snapshot.snapshot_resolution_sha256,
        }, record=alignment_publication_directory(destination) / "landmark-candidate.json",
            candidate_sha256=result["candidate_sha256"],
            predecessor_sha256=snapshot.transform.content_sha256)
    return service.apply_interaction_response(batch_id, request)




def submit_export(client, request):
    """

    Saves a landmark export request on the cluster and queues its Slurm operation.

    Args:
        client: Configured SSH/Slurm client for the analysis.
        request: Current alignment response selected in the desktop.

    Returns:
        Job description for the landmark-export operation, using the saved
        submission ID returned by the cluster.

    Raises:
        ValueError: The cluster does not return a valid saved-request ID.
    """

    from .cluster_annotation_jobs import _command
    from .cluster_annotation_jobs import _submit
    from .cluster_review_pages import _plain

    payload = json.dumps(_plain(request), allow_nan=False)
    result = json.loads(
        client._call(
            client.prepared_command(_command(client, "stage-landmark-review")), input=payload
        )
    )
    identifier = _identifier(result.get("submission_id"))
    return _submit(client, "landmark-export", _command(client, "landmark-export", identifier))


def submit_import(client, value):
    """

    Saves the local alignment step on the cluster and queues its fitting operation.

    Args:
        client: Configured SSH/Slurm client for the analysis.
        value: Returned alignment phase and the original review ID/checksum.

    Returns:
        Job description for landmark-import. A global step returns its fitted
        overlay; a final step publishes the selected transform for review.

    Raises:
        ValueError: The submission exceeds 1 MiB or the cluster does not return
            a valid ID for the saved point set.
    """

    from .cluster_annotation_jobs import _command
    from .cluster_annotation_jobs import _submit

    payload = json.dumps(value, allow_nan=False)
    if len(payload.encode()) > 1024 * 1024:
        raise ValueError("The landmark submission is too large.")
    result = json.loads(
        client._call(client.prepared_command(_command(client, "stage-landmarks")), input=payload)
    )
    identifier = _identifier(result.get("submission_id"))
    return _submit(client, "landmark-import", _command(client, "landmark-import", identifier))
