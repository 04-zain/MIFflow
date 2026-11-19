"""Read-only local sample projection for an already initialized cluster analysis.

The interface keeps editable researcher choices associated with the current image or
analysis request. Values are passed to the stage-specific validator before they become
accepted processing settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import Path, PurePosixPath
from dataclasses import replace
import hashlib, json, re
from .analysis_setup import create_analysis_setup_from_draft
from .manifest_draft import ManifestDraft, SampleDraft, ExecutionDraft, ProjectChoice
from .cluster_sample_projection import cluster_samples
from .execution_location import save_location

# ==============================================================================
#                                 Cluster reopen
# ==============================================================================


def peek_manifest(folder):
    source = Path(folder) / "analysis.manifest.json"
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Choose the saved analysis folder containing analysis.manifest.json.")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("The saved analysis has repeated fields.")
            result[key] = value
        return result

    with source.open("rb") as stream:
        body = stream.read(4 * 1024 * 1024 + 1)
    if len(body) > 4 * 1024 * 1024:
        raise ValueError("The saved analysis setup is too large.")
    value = json.loads(
        body,
        object_pairs_hook=unique,
        parse_constant=lambda _v: (_ for _ in ()).throw(
            ValueError("Invalid analysis setup number.")
        ),
    )
    if (
        not isinstance(value, dict)
        or value.get("schema_name") != "mif-batch-manifest"
        or not isinstance(value.get("batch_id"), str)
        or not isinstance(value.get("output_root"), str)
    ):
        raise ValueError("This folder does not contain a supported saved analysis.")
    return value, hashlib.sha256(body).hexdigest()


def _cluster_projection(client, folder, parent, mounted, *, local_id=None):
    folder = Path(folder)
    parent = Path(parent)
    if not parent.is_absolute() or not parent.is_dir() or parent.is_symlink():
        raise ValueError("Choose an existing local parent folder.")
    remote_folder = mounted.cluster_path(str(folder))
    local, local_sha = peek_manifest(folder)
    described = client.describe_analysis_folder(remote_folder)
    expected = {"schema", "manifest", "manifest_sha256", "normalized_manifest_sha256", "document"}
    if (
        not isinstance(described, dict)
        or set(described) != expected
        or described["schema"] != "cluster-analysis-description/v1"
    ):
        raise ValueError("The cluster did not provide a valid saved analysis.")
    if (
        described["manifest"] != client.profile.manifest
        or described["manifest_sha256"] != local_sha
    ):
        raise ValueError(
            "The mounted analysis differs from the analysis on this cluster connection."
        )
    document = described["document"]
    profile = client.profile
    if document["batch_id"] != profile.batch_id or document["output_root"] != profile.output_root:
        raise ValueError("The selected cluster analysis has a different name or location.")
    status = client.analysis_status()
    if status.get("batch_id") != profile.batch_id or not re.fullmatch(
        "[0-9a-f]{64}", str(status.get("state_head_sha256", ""))
    ):
        raise ValueError("The cluster could not verify saved progress for this analysis.")
    rows = []
    optional = (
        "validation_domain_id",
        "source_image_index",
        "source_series_index",
        "source_file_sha256",
        "phenotyping_group_id",
    )
    for item in document["samples"]:
        reference = item.get("paired_reference_image") or {}
        rows.append(
            SampleDraft(
                enabled=item.get("enabled", True),
                sample_id=item["sample_id"],
                slide_id=item["slide_id"],
                subject_id=item["subject_id"],
                source_mif_path=Path(mounted.local_path(item["source_mif_path"])),
                moving_reference_path=(
                    Path(mounted.local_path(reference["source_path"]))
                    if reference.get("source_path")
                    else None
                ),
                moving_reference_modality=reference.get("modality"),
                moving_reference_id=reference.get("reference_id"),
                moving_reference_source_image_id=reference.get("source_image_id"),
                moving_reference_source_metadata=reference.get("source_metadata", {}),
                overrides=item.get("overrides", {}),
                **{field: item.get(field) for field in optional},
            )
        )
    workspace = parent / (local_id or profile.batch_id)
    project = document.get("project_path") or document.get("create_project_path")
    draft = ManifestDraft(
        batch_id=workspace.name,
        validation_domain_id=document["validation_domain_id"],
        validation_domains=tuple(document.get("validation_domains", ())),
        output_root=parent,
        project_choice=ProjectChoice.OPEN,
        project_path=Path(mounted.local_path(project)),
        shared_analysis_config_path=workspace / "analysis.settings.json",
        execution=ExecutionDraft(**document["execution_config"]),
        samples=tuple(rows),
    )
    selection = {
        "host": profile.host,
        "manifest": profile.manifest,
        "output_root": profile.output_root,
        "samples": cluster_samples(draft, mounted),
    }
    digest = hashlib.sha256(
        json.dumps(selection, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    settings = {
        "mode": "streaming",
        "device": "auto",
        "remote": profile.to_mapping(),
        "mounted_images": mounted.to_mapping(),
        "paths_policy": "explicit",
        "remote_prepared": {
            "selection_sha256": digest,
            "result": {
                "batch_id": profile.batch_id,
                "manifest": profile.manifest,
                "sample_count": len(rows),
                "manifest_sha256": local_sha,
            },
        },
        "remote_opened": {
            "manifest_sha256": local_sha,
            "state_head_sha256": status["state_head_sha256"],
        },
    }
    return draft, document["shared_analysis_config"], settings


def reopen_cluster_analysis(client, folder, parent, mounted):
    """Compatibility API; the desktop now associates within Computing."""
    draft, analysis, settings = _cluster_projection(client, folder, parent, mounted)
    return create_analysis_setup_from_draft(
        draft, name=draft.batch_id, settings=analysis, computing_settings=settings
    )


def associate_cluster_analysis(client, folder, setup, mounted, choices):
    """Bind an empty local workspace to verified remote progress, preserving its name."""
    from .analysis_setup import _unchanged, _payload, _publish, save_analysis_setup
    from .execution_location import load_location, save_location

    if setup.draft.samples:
        raise ValueError(
            "Open an existing cluster analysis before adding samples to this local analysis."
        )
    _unchanged(setup)
    settings_path = setup.path.parent / "analysis.computing.json"
    current = load_location(settings_path)
    if (setup.path.parent / "analysis.manifest.json").exists() or any(
        current.get(k)
        for k in ("remote_prepared", "remote_receipt", "remote_opened", "submission_uncertain")
    ):
        raise ValueError(
            "This local analysis already has saved processing. Create a new local analysis to open another cluster analysis."
        )
    previous = settings_path.read_bytes() if settings_path.exists() else None
    draft, analysis, settings = _cluster_projection(
        client, folder, setup.draft.output_root, mounted, local_id=setup.draft.batch_id
    )
    if set(choices) - {"device", "precision", "mounted_host_override"}:
        raise ValueError("Unknown local computing choices.")
    settings.update(choices)
    updated = replace(setup, draft=draft, analysis_settings=analysis)
    body = _payload(updated.name, updated.draft, updated.analysis_settings)
    settings["remote_opened"]["local_setup_sha256"] = hashlib.sha256(body).hexdigest()
    _unchanged(setup)
    if (settings_path.read_bytes() if settings_path.exists() else None) != previous:
        raise ValueError(
            "Computing settings changed while opening the cluster analysis. Reopen Computing before continuing."
        )
    # Publish the remote mode first. Its expected local setup digest prevents
    # an interrupted association from being mistaken for a local runnable setup.
    saved = save_location(settings_path, settings)
    written = settings_path.read_bytes()
    try:
        updated = save_analysis_setup(updated)
    except BaseException:
        if settings_path.is_symlink() or settings_path.read_bytes() != written:
            raise ValueError(
                "Computing settings changed during opening. Reopen this local analysis to review its connection."
            )
        if previous is None:
            settings_path.unlink()
        else:
            _publish(settings_path, previous, replace_existing=True)
        raise
    return updated, saved
