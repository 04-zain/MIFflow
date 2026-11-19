"""Exchange a manual-alignment session between mounted viewing and cluster fitting.

We prepare a local project containing the selected image readers and protected
tissue outlines. Its first saved request supplies a starting pose and affine
settings. The returned cluster transform is copied into that session for global
review and optional local landmark editing. Each returned request retains the
original image-pair ticket. The final candidate uses the ordinary alignment
review; intermediate transforms stay with the editing session.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import Path
import hashlib, json, os, re, uuid
from .cluster_annotations import _sha
from .cluster_annotations import _new
from .cluster_annotations import _read
from mif_pipeline.registration.landmarks import validate_manual_alignment

# ==============================================================================
#                           Cluster landmark exchange
# ==============================================================================


def _layout(root):
    """

    Defines the local directories used to prepare the QuPath landmark scripts.

    Args:
        root: Directory reserved for the local viewing project or script preparation.

    Returns:
        ProjectLayout with the script, data, classifier, log and exchange paths
        expected by the existing QuPath launch functions.
    """

    from mif_pipeline.qupath_runtime.project import ProjectLayout

    return ProjectLayout(
        root,
        root / "project.qpproj",
        root / "scripts",
        root / "classifiers/pixel_classifiers",
        root / "classifiers/object_classifiers",
        root / "data",
        root / "exchange",
        root / "checkpoints",
        root / "provenance",
        root / "runs",
        root / "data/analysis-config.json",
        "{}",
    )


def run_native(runner, folder, request):
    """

    Executes one landmark operation with the current exchange and geometry scripts.

    We prepare the registered exchange, geometry and affine scripts, then create a
    new directory for this operation's request, result and process logs. The
    request includes the current exchange format and the selected geometry
    script's location and checksum. QuPath reads it to export the pair, prepare
    the local project, verify an existing project or fit returned landmarks.

    Args:
        runner: Configured QuPath runner on the computer performing this operation.
        folder: Parent directory for the prepared scripts and per-operation files.
        request: Operation mode and its selected image/project or landmark fields.
            The function supplies the current schema and geometry helper fields.

    Returns:
        Pair containing the decoded result and the operation directory. The
        latter retains its request and logs for diagnosing a failed operation.

    Raises:
        ValueError: A saved request or returned result is invalid.
        OSError: Required directories or files cannot be prepared. Native errors
            propagate through the runner with the saved QuPath log.
    """

    from mif_pipeline.qupath_runtime.scripts import materialize_script_bundle, bundle_script_path, QuPathScriptAsset as Asset

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    bundle = materialize_script_bundle(
        _layout(folder / "support"),
        (Asset.LANDMARK_EXCHANGE, Asset.PAIRED_GEOMETRY, Asset.AUTOMATIC_ELASTIX),
    )
    operation = folder / uuid.uuid4().hex
    operation.mkdir(mode=0o700)
    helpers = {
        name: bundle_script_path(bundle, asset)
        for name, asset in (
            ("geometry", Asset.PAIRED_GEOMETRY),
            ("algorithm", Asset.AUTOMATIC_ELASTIX),
        )
    }
    payload = {
        **request,
        "schema": "landmark-exchange/v2",
        "helpers": {
            name: {"path": str(path), "sha256": _sha(path)} for name, path in helpers.items()
        },
    }
    source = operation / "request.json"
    result = operation / "result.json"
    _new(source, payload)
    try:
        runner.run_evidence_script(
            project_file=Path(request.get("project_file", operation / "local.qpproj")),
            script=bundle_script_path(bundle, Asset.LANDMARK_EXCHANGE),
            request_paths=(source,),
            log_directory=operation / "logs",
            require_existing_project=False,
            evidence_path=result,
        )
    finally:
        if request["mode"] == "fit-global":
            # Retain fitted parameters and logs; release the temporary fitting images.
            for path in (operation / "global").rglob("*.mha"):
                if path.is_file() and not path.is_symlink():
                    path.unlink()
    return _read(result), operation


def ticket_digest(value):
    """

    Calculates the checksum of a saved landmark review description.

    We encode all fields except ticket_sha256 using sorted JSON keys and fixed
    separators. Excluding the checksum field lets the same calculation verify
    the complete saved description when it returns from the local computer.

    Args:
        value: Mapping containing the analysis review, image-pair descriptions,
            source-file facts and accepted tissue outlines.

    Returns:
        SHA-256 of the deterministically encoded review fields.
    """

    plain = {k: v for k, v in value.items() if k != "ticket_sha256"}
    return hashlib.sha256(
        json.dumps(plain, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def validate_ticket(ticket):
    """

    Checks the saved image-pair review before preparing or reopening local editing.

    We first require the current field set, session identifier and checksum.
    Each of the two image descriptions must provide its original project-entry
    ID, source URI, reader settings, dimensions, channel count, pixel type and
    physical X/Y calibration. The dimensions must be positive integers and the
    calibration must be finite and positive in µm/pixel.

    The cluster source locations must be absolute file URIs. We also check the
    recorded source-file values and the accepted tissue FeatureCollection before
    a viewing project can be built. This function checks the saved description;
    verify_local_project() subsequently opens the mounted readers and compares
    their actual image fields.

    Args:
        ticket: Saved cluster-landmark-session/v1 dictionary containing both
            selected images, their cluster review and tissue outlines.

    Returns:
        Checked review dictionary used to prepare or reopen its local project.

    Raises:
        ValueError: Fields, IDs, source locations, calibration, image dimensions
            or the review checksum are invalid or inconsistent.
    """

    required = {
        "schema",
        "ticket_id",
        "batch_id",
        "request",
        "snapshot_sha256",
        "source_files",
        "pair",
        "tissue_objects",
        "ticket_sha256",
    }
    if (
        not isinstance(ticket, dict)
        or set(ticket) != required
        or ticket["schema"] != "cluster-landmark-session/v1"
    ):
        raise ValueError("This landmark session has an unsupported format.")
    if not isinstance(ticket["ticket_id"], str) or not re.fullmatch(
        "[0-9a-f]{32}", ticket["ticket_id"]
    ):
        raise ValueError("This landmark session is invalid.")
    if (
        not isinstance(ticket["pair"], dict)
        or ticket.get("ticket_sha256") != ticket_digest(ticket)
        or set(ticket["pair"]) != {"fixed", "moving"}
    ):
        raise ValueError("The saved landmark session changed.")
    from math import isfinite
    from urllib.parse import urlsplit, unquote

    if (
        type(ticket["batch_id"]) is not str
        or not ticket["batch_id"]
        or not re.fullmatch("[0-9a-f]{64}", str(ticket["snapshot_sha256"]))
        or not isinstance(ticket["source_files"], dict)
        or set(ticket["source_files"]) != {"fixed", "moving"}
        or not isinstance(ticket["tissue_objects"], dict)
        or ticket["tissue_objects"].get("type") != "FeatureCollection"
    ):
        raise ValueError("The saved landmark image scope is incomplete.")
    from ..batch_cli import _response_text

    _response_text(json.dumps(ticket["request"], allow_nan=False))
    for source in ticket["source_files"].values():
        if (
            not isinstance(source, dict)
            or set(source) != {"device", "inode", "size", "modified_ns", "changed_ns"}
            or any(type(v) is not int or v < 0 for v in source.values())
        ):
            raise ValueError("The landmark source-image identity is invalid.")
    for row in ticket["pair"].values():
        if not isinstance(row, dict) or set(row) != {
            "project_entry_id",
            "image_name",
            "image_type",
            "source_uri",
            "server_builder",
            "signature",
        }:
            raise ValueError("The landmark image description is incomplete.")
        if any(
            type(row[key]) is not str or not row[key] or "\0" in row[key]
            for key in (
                "project_entry_id",
                "image_name",
                "image_type",
                "source_uri",
                "server_builder",
            )
        ):
            raise ValueError("The landmark image description is invalid.")
        uri = urlsplit(row["source_uri"])
        if (
            uri.scheme != "file"
            or uri.netloc
            or uri.query
            or uri.fragment
            or not uri.path.startswith("/")
            or ".." in Path(unquote(uri.path)).parts
        ):
            raise ValueError("The landmark image source is not a cluster file.")
        sig = row["signature"]
        if not isinstance(sig, dict) or set(sig) != {
            "width",
            "height",
            "channels",
            "pixel_type",
            "x_um",
            "y_um",
        }:
            raise ValueError("The landmark image dimensions are incomplete.")
        if (
            any(
                type(sig[k]) is not int or not 0 < sig[k] <= 2**31 - 1
                for k in ("width", "height", "channels")
            )
            or any(
                type(sig[k]) not in (int, float) or not isfinite(sig[k]) or sig[k] <= 0
                for k in ("x_um", "y_um")
            )
            or type(sig["pixel_type"]) is not str
            or not sig["pixel_type"]
        ):
            raise ValueError("The landmark image dimensions or pixel size are invalid.")
    if ticket["pair"]["fixed"]["project_entry_id"] == ticket["pair"]["moving"]["project_entry_id"]:
        raise ValueError("Choose two distinct images for landmark alignment.")
    return ticket


def prepare_local_project(runner, folder, local_images, ticket):
    """

    Prepares or reuses the exact local viewing project for an exported image pair.

    We check the exported review and resolve the existing mounted images for the
    fixed mIF and moving paired image. The native local operation replaces
    each source reader's cluster URI with its mounted URI, opens it and checks
    its dimensions, channels, pixel type and calibration. The accepted tissue
    outlines are copied into the fixed image and remain locked during editing.

    After the project is created, we prepare its current QuPath tools and save
    the original review, local entry IDs and mounted paths in landmark-session.json.
    These fields connect the local project to the later return and candidate view.
    Replaying a completed export reuses only a session with the same ticket and
    mounted paths; the ordinary editor launch verifies its actual image readers.
    If a new preparation fails, we remove its incomplete folder only after the
    runner confirms all native work has stopped. Existing folders are retained.

    Args:
        runner: Local QuPath runner used to create and inspect the viewing project.
        folder: Absolute directory reserved for this manual alignment session.
        local_images: Mapping with fixed and moving paths supplied by the mounted
            image folder.
        ticket: Checked cluster image-pair review and accepted tissue outlines.

    Returns:
        Path to the local project.qpproj used by the landmark editor.

    Raises:
        ValueError: An existing folder is incomplete or belongs to another session,
            mounted images are unavailable, or the returned pair differs.
        OSError: Preparing the project or saving its session fails.
    """

    from mif_pipeline.qupath_runtime.scripts import materialize_script_bundle, registered_script_assets

    # Keep the exported cluster review with the local project. Both the
    # image readers and a later returned point set must match this review.
    ticket = validate_ticket(ticket)
    folder = Path(folder)
    if not folder.is_absolute() or ".." in folder.parts or folder.is_symlink():
        raise ValueError("Choose a local folder for this landmark draft.")
    if set(local_images) != {"fixed", "moving"}:
        raise ValueError("Both images must be available through the mounted folder.")
    from mif_pipeline.io import image_identity_member

    for path in local_images.values():
        image_identity_member(path)
    sources = {key: str(path) for key, path in local_images.items()}
    project_folder = folder / "project"
    project = project_folder / "project.qpproj"
    if folder.exists():
        session = read_local_session(project)
        if session["ticket"] != ticket or session["local_images"] != sources:
            raise ValueError("This local alignment project belongs to a different review or image mount.")
        return project
    # Only this call's successful mkdir grants cleanup ownership. A pre-existing
    # folder never enters this block, including an incomplete earlier session.
    folder.mkdir(parents=True, mode=0o700)
    try:
        result, _ = run_native(
            runner,
            folder,
            {
                "mode": "local",
                "local_folder": str(project_folder),
                "local_images": sources,
                "pair": ticket["pair"],
                "tissue_objects": ticket["tissue_objects"],
                "ticket_sha256": ticket["ticket_sha256"],
            },
        )
        if result.get("project_file") != str(project) or set(result.get("entries", {})) != {
            "fixed",
            "moving",
        }:
            raise ValueError("The local alignment project is incomplete.")
        materialize_script_bundle(_layout(project_folder), registered_script_assets())
        _new(
            project_folder / "landmark-session.json",
            {"ticket": ticket, "entries": result["entries"], "local_images": sources},
        )
    except BaseException as error:
        if runner.is_quiescent:
            import shutil

            try:
                shutil.rmtree(folder)
            except OSError as cleanup_error:
                error.add_note(f"The incomplete local alignment folder was retained: {cleanup_error}")
        raise
    return project


def read_local_session(project):
    """

    Reads the cluster review and mounted image paths associated with a local landmark project.

    The project must have its adjacent landmark-session.json containing the
    original review, both local project-entry IDs and both mounted paths. We
    validate that saved description before using it to open or return an edit.

    Args:
        project: Local project.qpproj filename.

    Returns:
        Session dictionary containing ticket, entries and local_images.

    Raises:
        ValueError: The project is missing or the saved session fields and
            image-pair review are incomplete or invalid.
    """

    project = Path(project)
    if not project.is_file() or project.is_symlink():
        raise ValueError("The landmark draft is unavailable.")
    session = _read(project.parent / "landmark-session.json")
    if not isinstance(session, dict) or set(session) != {"ticket", "entries", "local_images"}:
        raise ValueError("The saved landmark draft is incomplete.")
    validate_ticket(session["ticket"])
    if set(session["entries"]) != {"fixed", "moving"} or set(session["local_images"]) != {
        "fixed",
        "moving",
    }:
        raise ValueError("The saved landmark image pair is incomplete.")
    return session


def verify_local_project(runner, project):
    """

    Reopens the local image pair and compares it with the exported cluster image descriptions.

    We combine each original reader description with its mounted URI and local
    entry ID, then ask QuPath to inspect both entries. Width, height, channel
    count, pixel type, X/Y calibration and the selected image within the file
    must match the retained review. The result must identify that same review
    before its session is returned.

    Args:
        runner: Local QuPath runner used for the verification operation.
        project: Existing local landmark project.

    Returns:
        Saved session after the actual local image readers have passed their checks.

    Raises:
        ValueError: A selected image, mounted reader or returned review identity
            differs from the saved image-pair description.
    """

    project = Path(project)
    session = read_local_session(project)
    ticket = session["ticket"]
    entries = {
        role: {
            "project_entry_id": session["entries"][role],
            "source_uri": Path(session["local_images"][role]).as_uri(),
            "signature": row["signature"],
            "server_builder": row["server_builder"],
        }
        for role, row in ticket["pair"].items()
    }
    result, _ = run_native(
        runner,
        project.parent / "checks",
        {
            "mode": "verify-local",
            "project_file": str(project),
            "project_sha256": _sha(project),
            "entries": entries,
            "ticket_sha256": ticket["ticket_sha256"],
        },
    )
    if result != {"verified": True, "ticket_sha256": ticket["ticket_sha256"]}:
        raise ValueError("The local image pair could not be verified.")
    return session


def collect_landmarks(runner, project):
    """Collect the explicitly saved manual-alignment phase for cluster fitting.

    We verify the mounted image pair and require the draft's ticket to match
    its local session. The editor marks a request ready only after an explicit
    save. The shared validator checks either starting-pose/global settings or
    the reviewed affine identity and optional local pairs in original pixels.

    Args:
        runner: Local QuPath runner used to verify the selected image readers.
        project: Local viewing project containing its saved phase request.

    Returns:
        The ticket ID/checksum and normalized alignment request.

    Raises:
        ValueError: The image pair differs, the current edit is unfinished, or
            the saved settings or local points are incomplete or invalid.
    """

    session = verify_local_project(runner, project)
    ticket = session["ticket"]
    draft = _read(Path(project).parent / "landmarks.json")
    if (
        not isinstance(draft, dict)
        or set(draft) != {"schema", "ticket_sha256", "ready_for_return", "alignment"}
        or draft["schema"] != "manual-alignment-draft/v1"
        or draft["ticket_sha256"] != ticket["ticket_sha256"]
    ):
        raise ValueError("The saved manual alignment belongs to another review.")
    # Saving an unfinished editor preserves points locally, but does not
    # authorize returning an earlier fit after those points were edited.
    if draft["ready_for_return"] is not True:
        raise ValueError("Reopen the draft and save the current alignment step before returning it.")
    sizes = {
        role: (row["signature"]["width"], row["signature"]["height"])
        for role, row in ticket["pair"].items()
    }
    alignment = validate_manual_alignment(
        draft["alignment"], fixed_size=sizes["fixed"], moving_size=sizes["moving"]
    )
    return {
        "ticket_id": ticket["ticket_id"],
        "ticket_sha256": ticket["ticket_sha256"],
        "alignment": alignment,
    }


def prepare_global_view(runner, project, result, mounted):
    """Copy the completed cluster affine into its local editing session.

    We bind the returned fit to the original ticket and image entries, then copy
    only its small coordinate transform through the mount. The completed copy is
    checked before its receipt replaces the selected global fit. Opening the
    editor subsequently verifies the mounted image readers once through the
    existing launch operation. This intermediate file enables global review and
    local landmark placement; it does not install a final image-pair transform.

    Args:
        runner: Local runner supplied by the shared file-operation interface.
        project: Existing local viewing project for this exported image pair.
        result: Completed manual-global-result/v1 returned by the cluster job.
        mounted: Cluster-to-local path mapping for the saved transform.

    Returns:
        The local project path, ready to reopen for global alignment review.

    Raises:
        ValueError: The result belongs to another pair or the transform differs.
        OSError: Reading the mounted transform or saving the local receipt fails.
    """
    project = Path(project)
    session = read_local_session(project)
    ticket = session["ticket"]
    fields = {"schema", "ticket_sha256", "global_fit_id", "global_transform_path",
              "global_transform_sha256", "alignment", "global_metrics",
              "fixed_entry_id", "moving_entry_id", "direction"}
    if (not isinstance(result, dict) or set(result) != fields
            or result["schema"] != "manual-global-result/v1"
            or result["ticket_sha256"] != ticket["ticket_sha256"]
            or result["direction"] != "moving-to-fixed-level0"
            or any(result[role + "_entry_id"] != ticket["pair"][role]["project_entry_id"]
                   for role in ("fixed", "moving"))
            or any(not isinstance(result[key], str)
                   or not re.fullmatch("[0-9a-f]{64}", result[key])
                   for key in ("global_fit_id", "global_transform_sha256"))
            or not isinstance(result["global_metrics"], dict)
            or not isinstance(result["global_transform_path"], str)):
        raise ValueError("The returned global affine belongs to another alignment session.")
    pair = ticket["pair"]
    alignment = validate_manual_alignment(
        result["alignment"],
        fixed_size=(pair["fixed"]["signature"]["width"], pair["fixed"]["signature"]["height"]),
        moving_size=(pair["moving"]["signature"]["width"], pair["moving"]["signature"]["height"]),
    )
    if alignment["stage"] != "global" or alignment != result["alignment"]:
        raise ValueError("The global result has different starting-position settings.")
    cluster_path = result["global_transform_path"]
    if not cluster_path.startswith("/") or ".." in cluster_path.split("/"):
        raise ValueError("The cluster global transform location is invalid.")
    source = Path(mounted.local_path(cluster_path))
    if (source.is_symlink() or not source.is_file()
            or source.stat().st_size > 16 * 1024 * 1024):
        raise ValueError("The mounted global transform is unavailable.")
    target = project.parent / ("global-affine-" + result["global_fit_id"] + ".json")
    expected = result["global_transform_sha256"]
    temporary = project.parent / (".global-affine-" + uuid.uuid4().hex + ".tmp")
    try:
        if target.exists():
            if target.is_symlink() or not target.is_file() or _sha(target) != expected:
                raise ValueError("The local global affine differs from its completed fit.")
        else:
            digest = hashlib.sha256()
            count = 0
            with source.open("rb") as reader, temporary.open("xb") as writer:
                while block := reader.read(1024 * 1024):
                    count += len(block)
                    if count > 16 * 1024 * 1024:
                        raise ValueError("The mounted global transform exceeds its size limit.")
                    digest.update(block)
                    writer.write(block)
                writer.flush()
                os.fsync(writer.fileno())
            if digest.hexdigest() != expected:
                raise ValueError("The mounted global affine changed before review.")
            os.link(temporary, target)
        receipt = {
            "schema": "manual-global-view/v1", "ticket_sha256": ticket["ticket_sha256"],
            "global_fit_id": result["global_fit_id"], "global_transform_sha256": expected,
            "global_transform_file": target.name, "alignment": alignment,
            "global_metrics": result["global_metrics"],
        }
        staged = project.parent / (".global-view-" + uuid.uuid4().hex + ".json")
        try:
            _new(staged, receipt)
            os.replace(staged, project.parent / "global-fit.json")
            from .cluster_annotations import _sync
            _sync(project.parent)
        finally:
            staged.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)
    return project


def prepare_candidate_view(runner, project, pending, mounted):
    """

    Makes the cluster's proposed transform available in the same local image-pair project.

    We require the current full-image registration awaiting review, then compare
    its image-entry IDs, original source paths and X/Y calibration with the local
    session's cluster descriptions. The mounted-folder mapping supplies the local
    path to the cluster's transform file.

    QuPath verifies the local readers again and copies the transform into the
    fixed entry's expected transform filename. Its contents must match the
    cluster's saved checksum. An already present identical transform is reused;
    a different one requires resolving the earlier candidate before replacement.

    Args:
        runner: Local QuPath runner used to verify and prepare the candidate view.
        project: Local viewing project for the selected cluster image pair.
        pending: Registration record currently awaiting acceptance in the cluster analysis.
        mounted: Mount mapping that converts the cluster transform path to its
            accessible local path.

    Returns:
        Candidate-overlay settings containing local fixed/moving entry IDs and
        the checked transform checksum for the paired-image viewer.

    Raises:
        ValueError: The registration is not pending, describes another image
            pair/calibration, or its file differs from the requested candidate.
    """

    from mif_pipeline.registration.backend import RegistrationRecord
    from ..alignment_context import _plain
    from urllib.parse import urlsplit, unquote

    record = RegistrationRecord.from_mapping(_plain(pending))
    if (
        record.acceptance_state != "pending_review"
        or record.coordinate_pre_transform is not None
        or record.coordinate_post_transform is not None
    ):
        raise ValueError("Open the current full-image alignment candidate for review.")
    session = verify_local_project(runner, project)
    ticket = session["ticket"]
    for role, row in ticket["pair"].items():
        calibration = getattr(record, role + "_physical_calibration")
        if (
            getattr(record, role + "_project_entry_id") != row["project_entry_id"]
            or getattr(record, role + "_image_path").as_uri() != row["source_uri"]
            or calibration.x_um_per_px != row["signature"]["x_um"]
            or calibration.y_um_per_px != row["signature"]["y_um"]
        ):
            raise ValueError("The candidate belongs to a different image pair or calibration.")
    # Transfer only the saved coordinate transform through the mount.
    # The local viewers continue to read the original image files.
    candidate = Path(mounted.local_path(str(record.transform_file)))
    entries = {
        role: {
            "project_entry_id": session["entries"][role],
            "source_uri": Path(session["local_images"][role]).as_uri(),
            "signature": row["signature"],
            "server_builder": row["server_builder"],
        }
        for role, row in ticket["pair"].items()
    }
    result, _ = run_native(
        runner,
        Path(project).parent / "checks",
        {
            "mode": "candidate",
            "project_file": str(project),
            "project_sha256": _sha(project),
            "entries": entries,
            "ticket_sha256": ticket["ticket_sha256"],
            "candidate_source": str(candidate),
            "candidate_sha256": record.transform_file_sha256,
        },
    )
    if result != {"verified": True, "ticket_sha256": ticket["ticket_sha256"]}:
        raise ValueError("The registration candidate could not be prepared for review.")
    return {
        "mode": "candidate_overlay",
        "fixed_entry_id": session["entries"]["fixed"],
        "moving_entry_id": session["entries"]["moving"],
        "candidate_transform_sha256": record.transform_file_sha256,
    }
