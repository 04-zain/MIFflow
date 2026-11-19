"""

Prepares the automatic alignment that we subsequently inspect and accept in Run.

We obtain the current mIF image, paired image and accepted tissue regions
from the pending alignment step. The selected parameters specify global
affine fitting and, when enabled, local B-spline refinement. QuPath reads
the original image views and runs Elastix; this module then records the
proposed transform beside the fixed image's project data. Its fitting description
is retained separately in the project's registration folder.

The fitting description includes the image-pair snapshot, tissue selection,
complete parameter values and transform checksum. Repeating the same request
can reuse that proposal after these values are checked. Accepting the
alignment remains a separate review action so fitting alone does not advance
the analysis past its required tissue comparison.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

import hashlib, json
from mif_pipeline.registration.automatic import AutomaticAlignmentSettings, estimate_candidate
from mif_pipeline.qupath_runtime.desktop_bootstrap import prepare_desktop_bootstrap

# ==============================================================================
#                           Batch automatic alignment
# ==============================================================================


def prepare_automatic_alignment(store, runner, request, options):
    """

    Fits or reuses the automatic alignment associated with the current image-pair review.

    We resolve the entered parameters and selected image information, then
    calculate a checksum from the image-pair snapshot, complete settings and
    tissue-region IDs. An existing proposal is reused only when its recorded
    settings checksum and current transform contents match these values.

    For a new proposal, we prepare the registered affine, B-spline and coordinate
    scripts and run the selected automatic route. Before saving the result, we
    check that the analysis is still at the same recorded step. Shared proposal
    publication retains any precisely admitted predecessor transform and its
    fitting description before replacing them. Saving failure restores those
    prior files; final acceptance remains a separate review action.

    Args:
        store: Current analysis-state store. The caller holds the analysis's
            write permission for this operation and supplies its pending review.
        runner: Configured QuPath runner used to read the image pair and execute
            the native registration scripts.
        request: Response associated with the current alignment step, including
            the analysis state that the form was built from.
        options: Complete automatic affine and optional B-spline settings, with
            an optional tools_directory locating Elastix and Transformix.

    Returns:
        Saved proposal description containing the settings checksum, transform
        checksum and native fitting result. It is used by the alignment review.

    Raises:
        ValueError: An existing proposal differs, the image-pair inputs or
            analysis step changed, or the fitting result fails its checks.
        FileExistsError: Another file already occupies a reserved destination.
        OSError: Preparing, writing, linking or cleaning up the result files fails.
    """

    settings = dict(options)
    tools = settings.pop("tools_directory", None)
    settings = AutomaticAlignmentSettings.from_mapping(settings)
    from .alignment_context import current_alignment_context
    from .cluster.cluster_landmarks import (
        alignment_publication_directory, publish_candidate, _sha, _read,
    )

    head, snapshot, project, destination, tissue_ids, _ = current_alignment_context(store, request)
    # Associate this proposal with the exact image pair, selected tissue
    # and resolved settings used by the fit.
    recipe = {
        "snapshot_sha256": snapshot.snapshot_resolution_sha256,
        "settings": settings.to_mapping(),
        "tissue_object_ids": tissue_ids,
    }
    recipe_sha = hashlib.sha256(
        json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt = alignment_publication_directory(destination) / "automatic.json"
    # A repeated request can reuse its completed proposal instead of
    # rerunning the optimizer, provided the settings and transform still match.
    if destination.is_symlink() or receipt.is_symlink():
        raise ValueError("The saved alignment destination is linked. Restore its original file.")
    current_sha = _sha(destination) if destination.exists() else None
    if current_sha is not None and receipt.is_file():
        saved = _read(receipt)
        if saved.get("recipe_sha256") == recipe_sha and saved.get("transform_sha256") == current_sha:
            return saved
    if current_sha != snapshot.transform.content_sha256:
        raise ValueError("The saved transform changed since this review. Reopen alignment setup.")
    bootstrap = prepare_desktop_bootstrap(project)
    if bootstrap is None:
        raise ValueError(
            "Open this pipeline project with the current registered tools before alignment"
        )
    candidate, result = estimate_candidate(
        runner=runner,
        project_file=project,
        snapshot=snapshot.to_mapping(),
        tissue_object_ids=tissue_ids,
        settings=settings,
        tools_directory=tools,
        helpers={
            "algorithm": bootstrap.bundle_root / "registration/automatic_elastix.groovy",
            "geometry": bootstrap.bundle_root / "registration/paired_geometry.groovy",
            "bspline": bootstrap.bundle_root / "registration/automatic_bspline.groovy",
        },
        driver=bootstrap.bundle_root / "registration/automatic_alignment_entry.groovy",
    )
    if store.load_head().sha256 != head.sha256:
        raise ValueError("The analysis changed during automatic alignment")
    # Save the proposal for the pending alignment review. Final acceptance
    # is recorded separately after we inspect the image correspondence.
    saved = {
        "schema": "automatic-alignment-publication/v1",
        "recipe_sha256": recipe_sha,
        "transform_sha256": result["candidate_sha256"],
        "result": result,
    }
    publish_candidate(destination, candidate, saved, record=receipt,
                      candidate_sha256=result["candidate_sha256"],
                      predecessor_sha256=snapshot.transform.content_sha256)
    return saved

