"""Bind a reviewed cluster collection to verified local viewing locations."""
from dataclasses import replace
import json
from pathlib import Path

from mif_pipeline.io.aligned_collection import (
    AlignedSourceCollection, MAX_DESCRIPTOR_BYTES, _unique,
    write_aligned_source_collection,
)
from mif_pipeline.io.backends import snapshot_opened_source_identity


def prepare_mounted_collection(*, source, remote_path, expected_sha256, mounted, directory):
    """Verify the reviewed descriptor and member content, then replace only locators/stat facts.

    The original ticket remains the cluster authority. The returned descriptor
    belongs only to its local viewing draft; readers retain their usual freshness
    checks against the newly captured local identities.
    """
    source = Path(source)
    expected_local = Path(mounted.local_path(remote_path))
    if source.absolute() != expected_local.absolute():
        raise ValueError("The collection descriptor differs from its configured mount location.")
    if source.is_symlink() or not source.is_file():
        raise FileNotFoundError(f"The mounted collection descriptor is unavailable: {source}")
    if source.stat().st_size > MAX_DESCRIPTOR_BYTES:
        raise ValueError("The mounted collection descriptor exceeds its size limit.")
    source_identity = snapshot_opened_source_identity(source)
    if source_identity.file_sha256 != expected_sha256:
        raise ValueError("The mounted collection descriptor differs from the current review. Reopen the review.")
    with source.open("rb") as stream:
        body = stream.read(MAX_DESCRIPTOR_BYTES + 1)
    if len(body) > MAX_DESCRIPTOR_BYTES:
        raise ValueError("The mounted collection descriptor exceeds its size limit.")
    mapping = json.loads(body, object_pairs_hook=_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite collection value")))
    if type(mapping) is not dict or type(mapping.get("members")) is not list:
        raise ValueError("The mounted collection descriptor has invalid member records.")
    # Translate POSIX cluster paths before parsing Path-backed records, including
    # on Windows where a cluster's /data/... path is not a local absolute path.
    for member in mapping["members"]:
        if (type(member) is not dict or type(member.get("identity")) is not dict
                or member.get("path") != member["identity"].get("resolved_path")):
            raise ValueError("The collection member location differs from its reviewed identity.")
        local = mounted.local_path(member["path"])
        member["path"] = local
        member["identity"]["resolved_path"] = local
    collection = AlignedSourceCollection.from_mapping(mapping)
    members = []
    for member in collection.members:
        try:
            identity = snapshot_opened_source_identity(member.path)
        except FileNotFoundError as error:
            raise FileNotFoundError(f"The mounted collection member is unavailable: {member.path}") from error
        if identity.file_sha256 != member.file_sha256:
            raise ValueError(f"The mounted collection member differs from the current review: {member.path}. Restore the reviewed image or reopen its inspection.")
        members.append(replace(member, path=identity.resolved_path, identity=identity))
    snapshot_opened_source_identity(source, retained=source_identity)
    for member in members:
        snapshot_opened_source_identity(member.path, retained=member.identity)
    projected = write_aligned_source_collection(
        Path(directory) / "mounted.mif-source.json", replace(collection, members=tuple(members)))
    local_identity = snapshot_opened_source_identity(projected)
    return projected, {
        "schema_name": "mif-mounted-source-collection-binding", "schema_version": 1,
        "source_path": remote_path, "source_sha256": expected_sha256,
        "mounted_source_path": str(source), "local_path": str(projected),
        "local_sha256": local_identity.file_sha256,
    }
