"""Read a cluster analysis by its existing folder, without preparing or running it."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import Path
import hashlib, stat
from ..batch_manifest import BatchManifest, validate_manifest

# ==============================================================================
#                                 Cluster reopen
# ==============================================================================


def describe_analysis(folder):
    folder = Path(folder)
    if (
        not folder.is_absolute()
        or ".." in folder.parts
        or folder.is_symlink()
        or not folder.is_dir()
    ):
        raise ValueError("Choose the existing analysis folder on the cluster.")
    source = folder / "analysis.manifest.json"
    if source.is_symlink() or not source.is_file():
        raise ValueError("This folder has no saved analysis.manifest.json.")
    before = source.stat()
    if before.st_size > 4 * 1024 * 1024:
        raise ValueError("This analysis setup exceeds the desktop opening size limit.")
    with source.open("rb") as stream:
        body = stream.read(4 * 1024 * 1024 + 1)
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if len(body) > 4 * 1024 * 1024 or identity(source.stat()) != identity(before):
        raise ValueError("The saved analysis changed while it was being opened.")
    validated = validate_manifest(
        BatchManifest.from_json_bytes(body), allow_materialized_create_target=True
    )
    manifest = validated.manifest
    if folder != manifest.output_root / manifest.batch_id:
        raise ValueError(
            "The selected folder differs from the location recorded for this analysis."
        )
    return {
        "schema": "cluster-analysis-description/v1",
        "manifest": str(source),
        "manifest_sha256": hashlib.sha256(body).hexdigest(),
        "normalized_manifest_sha256": validated.normalized_manifest_sha256,
        "document": manifest.to_mapping(),
    }
