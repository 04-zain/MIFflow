"""Materialize a source collection on its execution host from mapped paths."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from mif_pipeline.io.aligned_collection import (AlignedSourceCollection, SCHEMA_NAME,
    load_aligned_source_collection, write_aligned_source_collection)
from mif_pipeline.io.backends import snapshot_opened_source_identity


def materialize_collection_request(request, workspace):
    """Verify member content once, retaining this host's filesystem identities.

    Transport has paths and expected content hashes, never local inode authority.
    An identical retry reuses the owned descriptor after cheap identity checks.
    """
    if type(request) is not dict or request.get("schema_name") != "mif-aligned-source-collection-request":
        raise ValueError("Invalid aligned source collection request")
    body = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(body) > 1024 * 1024:
        raise ValueError("Aligned collection request exceeds 1 MiB")
    value = json.loads(body)
    if type(value.get("members")) is not list:
        raise ValueError("Aligned collection request requires members")
    for member in value["members"]:
        if type(member) is not dict or set(member) != {"path", "image_index", "series_index", "file_sha256", "channel_count"}:
            raise ValueError("Aligned collection request cannot transport local filesystem identities")
        member["identity"] = dict(resolved_path=member["path"], file_sha256=member["file_sha256"],
            size_bytes=0, modified_time_ns=0, device_id=0, inode_id=0)
    value["schema_name"] = SCHEMA_NAME
    declared = AlignedSourceCollection.from_mapping(value)
    path = Path(workspace) / "source_collections" / (hashlib.sha256(body).hexdigest() + ".mif-source.json")
    if path.exists():
        existing = load_aligned_source_collection(path)
        comparison = existing.to_mapping()
        comparison["schema_name"] = "mif-aligned-source-collection-request"
        for member in comparison["members"]:
            del member["identity"]
        if comparison != request:
            raise ValueError("Existing remote collection differs from the submitted files")
        for member in existing.members:
            snapshot_opened_source_identity(member.path, retained=member.identity)
        return path
    members = []
    for member in declared.members:
        identity = snapshot_opened_source_identity(member.path)
        if identity.file_sha256 != member.file_sha256:
            raise ValueError(f"The cluster source differs from the selected image: {member.path}")
        members.append(replace(member, path=identity.resolved_path, identity=identity))
    return write_aligned_source_collection(path, replace(declared, members=tuple(members)))
