"""Publish immutable image generations with one atomic selection record.

For Zarr, the image artifact digest identifies completed root metadata, including
its generation ID. It is deliberately not a recursive pixel-content checksum.
The writer owns shard completeness and durability before this boundary.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable

from .publication_lock import _AuthorityPublicationLock
from .output_publication_records import (
    OutputPublicationArtifact, OutputPublicationManifest, OutputPublicationPointer,
    PublishedUnmixingOutput, build_manifest, build_pointer, canonical_json,
    manifest_from_bytes, pointer_from_bytes, validate_sha256,
)


def authority_pointer_path(image_path: str | Path) -> Path:
    image = Path(image_path)
    return image.with_name(f".{image.name}.publication.json")


def _paths(image_path, provenance_path, clipping_evidence_path):
    image = Path(image_path).absolute()
    provenance = Path(provenance_path).absolute()
    evidence = (Path(clipping_evidence_path).absolute() if clipping_evidence_path is not None
                else image.with_name(f"{image.name}.clipping-evidence.json"))
    if not image.name.lower().endswith(".ome.zarr"):
        raise ValueError("Output image must end in .ome.zarr")
    if len({p.parent.resolve() for p in (image, provenance, evidence)}) != 1:
        raise ValueError("Output publication paths must be siblings")
    if len({p.name for p in (image, provenance, evidence)}) != 3:
        raise ValueError("Output publication names must be distinct")
    return image, provenance, evidence


def _flush_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write(path, raw):
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _digest(path):
    """Hash one persisted boundary artifact without scanning image payloads."""
    record = path / "zarr.json" if path.is_dir() else path
    digest = hashlib.sha256()
    length = 0
    with record.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            length += len(block)
    if path.is_dir():
        root = json.loads(record.read_bytes())
        processing = root.get("attributes", {}).get("mif_pipeline", {})
        if (root.get("zarr_format") != 3 or processing.get("schema") != "mif-processed-image/v1"
                or processing.get("complete") is not True or not processing.get("generation_id")):
            raise ValueError("Cannot publish an incomplete processed image generation")
    return digest.hexdigest(), length


def _result(pointer, manifest, pointer_path, generation, *, reused=False):
    by_role = {a.role: generation / a.relative_name for a in manifest.artifacts}
    return PublishedUnmixingOutput(pointer, manifest, pointer_path, generation,
        by_role["image"], by_role["provenance"], by_role.get("clipping_evidence"),
        reused=reused)


def _read_generation(generation, image, provenance, evidence):
    raw = (generation / "manifest.json").read_bytes()
    manifest = manifest_from_bytes(raw)
    if generation.name != manifest.generation_id:
        raise ValueError("Generation path disagrees with its manifest")
    aliases = dict(image=image.name, provenance=provenance.name, clipping_evidence=evidence.name)
    for artifact in manifest.artifacts:
        if artifact.alias_name != aliases[artifact.role]:
            raise ValueError("Generation belongs to different output paths")
        if _digest(generation / artifact.relative_name) != (artifact.content_sha256, artifact.byte_length):
            raise ValueError(f"Published {artifact.role} differs from its manifest")
    return manifest, raw


def load_unmixing_output_publication(*, image_path, provenance_path, clipping_evidence_path=None):
    image, provenance, evidence = _paths(image_path, provenance_path, clipping_evidence_path)
    selected = authority_pointer_path(image)
    pointer = pointer_from_bytes(selected.read_bytes())
    root_name = f".{image.name}.publication-generations"
    if Path(pointer.manifest_relative_path).parts[0] != root_name:
        raise ValueError("Selected generation belongs to another image")
    generation = selected.parent / root_name / pointer.generation_id
    manifest, raw = _read_generation(generation, image, provenance, evidence)
    if hashlib.sha256(raw).hexdigest() != pointer.manifest_sha256 or manifest.request_sha256 != pointer.request_sha256:
        raise ValueError("Selected manifest identity differs from its pointer")
    return _result(pointer, manifest, selected, generation)


def publish_unmixing_output(*, request_sha256: str, image_path: str | Path,
        provenance_path: str | Path, image_writer: Callable[[Path], object],
        provenance_bytes: bytes, clipping_evidence_path=None,
        clipping_evidence_bytes: bytes | None = None, overwrite: bool = False):
    validate_sha256(request_sha256, "request_sha256")
    image, provenance, evidence = _paths(image_path, provenance_path, clipping_evidence_path)
    image.parent.mkdir(parents=True, exist_ok=True)
    selected = authority_pointer_path(image)
    root = image.with_name(f".{image.name}.publication-generations")
    with _AuthorityPublicationLock(selected) as lock:
        current = (load_unmixing_output_publication(image_path=image, provenance_path=provenance,
                   clipping_evidence_path=evidence) if selected.exists() else None)
        if current is not None and current.manifest.request_sha256 == request_sha256:
            return replace(current, reused=True)
        if not overwrite and (current is not None or image.exists() or provenance.exists()):
            raise FileExistsError("Output already exists; overwrite must be explicitly selected")
        root.mkdir(exist_ok=True)
        generation = root / request_sha256
        if generation.exists():
            # A crash after generation installation but before pointer replacement
            # leaves reusable completed work, while the old selection stays valid.
            manifest, raw = _read_generation(generation, image, provenance, evidence)
            expected = {"provenance": provenance_bytes, "clipping_evidence": clipping_evidence_bytes}
            actual = {a.role: a for a in manifest.artifacts}
            for role, payload in expected.items():
                if payload is None:
                    if role in actual:
                        raise ValueError("Retry evidence differs from completed generation")
                elif role not in actual or hashlib.sha256(payload).hexdigest() != actual[role].content_sha256:
                    raise ValueError("Retry payload differs from completed generation")
        else:
            staging = Path(tempfile.mkdtemp(prefix=".preparing-", dir=root))
            try:
                output = staging / "image.ome.zarr"
                image_writer(output)
                if not output.is_dir():
                    raise ValueError("Processed image writer must produce a completed Zarr directory")
                _write(staging / "provenance.json", provenance_bytes)
                entries = [("image", output, image), ("provenance", staging / "provenance.json", provenance)]
                if clipping_evidence_bytes is not None:
                    _write(staging / "clipping-evidence.json", clipping_evidence_bytes)
                    entries.append(("clipping_evidence", staging / "clipping-evidence.json", evidence))
                artifacts = tuple(OutputPublicationArtifact(role, path.name, alias.name, *_digest(path))
                                  for role, path, alias in entries)
                manifest = build_manifest(request_sha256, artifacts, current.manifest.generation_id if current else None)
                raw = canonical_json(manifest.to_mapping())
                _write(staging / "manifest.json", raw)
                _flush_directory(staging)
                os.replace(staging, generation)
                _flush_directory(root)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        pointer = build_pointer(root.name, manifest, raw)
        fd, name = tempfile.mkstemp(prefix=f".{image.name}.select-", dir=image.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical_json(pointer.to_mapping()))
                stream.flush()
                os.fsync(stream.fileno())
            with lock.authority_install_fence():
                os.replace(temporary, selected)
            _flush_directory(image.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return _result(pointer, manifest, selected, generation)


__all__ = ("OutputPublicationArtifact", "OutputPublicationManifest", "OutputPublicationPointer",
           "PublishedUnmixingOutput", "authority_pointer_path", "load_unmixing_output_publication",
           "publish_unmixing_output")
