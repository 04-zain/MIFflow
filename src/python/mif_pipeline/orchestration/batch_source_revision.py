"""Correct one image/series while preserving prior source-owned files and objects."""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import json
from mif_pipeline.io import SourceSeriesInventory, bind_source_series_selection
from mif_pipeline.io.backends.base import OpenedSourceIdentity
from .batch_manifest import BatchManifestError, ValidatedBatchManifest, revalidate_retained_manifest

SOURCE_CORRECTION_KINDS = frozenset({'inspection', 'reference_unmixing', 'analysis_preflight'})
SOURCE_FIELDS = ('source_mif_path', 'source_image_index', 'source_series_index', 'source_file_sha256')

@dataclass(frozen=True, slots=True)
class SourceSelection:
    """Reuse the IO-owned opened file identity and selected image/series indices."""
    source_identity: OpenedSourceIdentity
    source_image_index: int
    source_series_index: int

    @classmethod
    def from_inventory(cls, inventory: SourceSeriesInventory, candidate_id: str) -> 'SourceSelection':
        """Bind the selected eligible image to the existing inspected source record."""
        selection = bind_source_series_selection(inventory, candidate_id=candidate_id, allow_pending_organization=True)
        return cls(inventory.source_identity, selection.source_image_index, selection.source_series_index)

    def require_current_file(self) -> None:
        """Check the inspected file's retained filesystem facts before applying its selection."""
        identity = self.source_identity
        path = identity.resolved_path
        if path.is_symlink() or not path.is_file():
            raise BatchManifestError('The inspected image is unavailable. Choose it and inspect it again.')
        observed = path.stat()
        if (observed.st_size, observed.st_mtime_ns, observed.st_dev, observed.st_ino) != (
            identity.size_bytes, identity.modified_time_ns, identity.device_id, identity.inode_id,
        ):
            raise BatchManifestError('The image changed after inspection. Inspect it again before applying this correction.')

    def to_mapping(self) -> dict:
        return dict(zip(SOURCE_FIELDS, (str(self.source_identity.resolved_path),
            self.source_image_index, self.source_series_index, self.source_identity.file_sha256)))


def source_revision_sample(previous: ValidatedBatchManifest, current: ValidatedBatchManifest,
                           source_sha256_by_sample=None) -> str:
    """Admit exactly one source selection while retaining all analysis and sample settings."""
    before, after = previous.manifest.to_mapping(), current.manifest.to_mapping()
    if len(before['samples']) != len(after['samples']):
        raise BatchManifestError('Source correction must retain every sample.')
    changed = []
    for old, new in zip(before['samples'], after['samples'], strict=True):
        old_selection = {key: old.pop(key, None) for key in (*SOURCE_FIELDS, "processing_root")}
        new_selection = {key: new.pop(key, None) for key in (*SOURCE_FIELDS, "processing_root")}
        if old_selection != new_selection:
            if old != new or not new['enabled']:
                raise BatchManifestError('Source correction must retain the enabled sample and all its settings.')
            old_sha = old_selection['source_file_sha256']
            if source_sha256_by_sample is not None:
                old_sha = source_sha256_by_sample[new['sample_id']]
            if (old_selection['source_mif_path'] == new_selection['source_mif_path']
                    and old_sha is not None and old_sha != new_selection['source_file_sha256']):
                raise BatchManifestError('The original file changed in place. Choose a different image file; retained work must keep its original source.')
            changed.append(new['sample_id'])
    if before != after or len(changed) != 1:
        raise BatchManifestError('Change exactly one image or series and retain every other analysis setting.')
    return changed[0]


def source_review_revision(snapshot, node):
    """Reuse the current inspection's retained terminal for a source return."""
    from .batch_settled_review_revision import SettledReviewRevision
    attempts = snapshot.head.attempt_ids_by_node.get(node.node_id, ())
    if not attempts:
        raise BatchManifestError("The selected source has no retained inspection attempt to correct.")
    prior = node.settled_review_revision
    return SettledReviewRevision(node.node_id, 1 if prior is None else prior.revision + 1,
        snapshot.head.terminal_attempt_sha256_by_attempt_id[attempts[-1]])


def source_processing_root(manifest, sample_id, revision, workspace_root=None):
    """Place this source return in its existing task namespace below the workspace."""
    root = manifest.manifest.output_root / manifest.manifest.batch_id if workspace_root is None else workspace_root
    return root / "samples" / sample_id / "sources" / f"{revision.predecessor_node_id}-{revision.revision}"


def revised_source_manifest(previous: ValidatedBatchManifest, sample_id: str,
                            selection: SourceSelection, *, revision, source_sha256_by_sample=None, state_store=None) -> ValidatedBatchManifest:
    """Build a new retained manifest through the existing strict manifest reader."""
    if type(selection) is not SourceSelection:
        raise BatchManifestError('Choose an inspected image and eligible series.')
    selection.require_current_file()
    if state_store is not None and (state_store.load_head().operational_rebinding_sha256 is not None
            or state_store.layout.root != previous.manifest.output_root / previous.manifest.batch_id):
        raise BatchManifestError("Correct the image in the original analysis folder; relocated source correction is not available.")
    mapping = previous.manifest.to_mapping()
    matches = [item for item in mapping['samples'] if item['sample_id'] == sample_id]
    if len(matches) != 1:
        raise BatchManifestError('The source-correction sample is no longer current.')
    matches[0].update(selection.to_mapping())
    matches[0]["processing_root"] = str(source_processing_root(previous, sample_id, revision,
        None if state_store is None else state_store.layout.root))
    raw = json.dumps(mapping, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    current = revalidate_retained_manifest(raw, raw)
    source_revision_sample(previous, current, source_sha256_by_sample)
    return current


def source_history_entries(project_path, *, metadata_bytes=None):
    """Read the existing native history index and reject active or ambiguous entries."""
    import json
    import re
    from mif_pipeline.qupath_runtime.catalogue import _read_bounded_relative_regular_file
    project_bytes = _read_bounded_relative_regular_file(project_path.parent, Path(project_path.name),
        max_bytes=16 * 1024 * 1024, label="current source-history project")
    from mif_pipeline.qupath_runtime.project_content import _decode, _unique_object
    project = _decode(project_bytes)
    if metadata_bytes is None:
        metadata_bytes = _read_bounded_relative_regular_file(
            project_path.parent, Path("data/metadata.json"),
            max_bytes=16 * 1024 * 1024, label="QuPath project metadata",
        )
    encoded = _decode(metadata_bytes).get("mif_pipeline.source_history_v1")
    rows = [] if encoded is None else json.loads(encoded, object_pairs_hook=_unique_object,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Source history contains a nonfinite value.")))
    if not isinstance(rows, list):
        raise ValueError("Saved source history must be a list.")
    seen = set()
    active = {str(item["entryID"]) for item in project.get("images", [])}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"mutation_request_sha256", "project_entry_id"}
                or not isinstance(row["mutation_request_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", row["mutation_request_sha256"]) is None
                or not isinstance(row["project_entry_id"], str)
                or re.fullmatch(r"[0-9]+", row["project_entry_id"]) is None
                or row["mutation_request_sha256"] in seen or row["project_entry_id"] in active):
            raise ValueError("Saved source history differs from its retained inactive entry.")
        seen.add(row["mutation_request_sha256"])
    return rows


def source_history_members(project_path):
    """Select saved old project views and native hierarchies from project metadata."""
    import json
    from .batch_project import source_project_history_path
    from mif_pipeline.qupath_runtime.catalogue import _read_bounded_relative_regular_file, _ENTRY_HIERARCHY_MAX_BYTES
    metadata_bytes = _read_bounded_relative_regular_file(
        project_path.parent, Path("data/metadata.json"),
        max_bytes=16 * 1024 * 1024, label="QuPath project metadata",
    )
    rows = source_history_entries(project_path, metadata_bytes=metadata_bytes)
    members = {"project/metadata.json": metadata_bytes}
    for row in rows:
        digest, entry_id = row["mutation_request_sha256"], row["project_entry_id"]
        history_path = source_project_history_path(project_path, digest)
        history_bytes = _read_bounded_relative_regular_file(project_path.parent, Path(history_path.name),
            max_bytes=16 * 1024 * 1024, label="saved source project")
        from mif_pipeline.qupath_runtime.project_content import _decode
        history = _decode(history_bytes)
        if sum(str(item["entryID"]) == entry_id for item in history.get("images", [])) != 1:
            raise ValueError("Saved source project does not contain its exact previous image.")
        data_relative = Path("data") / entry_id / "data.qpdata"
        members[f"project/source-history/{digest}/project.qpproj"] = history_bytes
        members[f"project/source-history/{digest}/{data_relative.as_posix()}"] = _read_bounded_relative_regular_file(
            project_path.parent, data_relative, max_bytes=_ENTRY_HIERARCHY_MAX_BYTES, label="saved source image data")
    return members


def _historical_image_view(history, entry_id, view_path, effective_locators):
    """Use the admitted image generation in one derived, single-entry project.

    Original project and hierarchy bytes remain archived. QuPath passes this
    entry's builder to PathIO when it reads the unchanged hierarchy, so moving
    the image needs no rewrite of annotations, cells or their result references.
    """
    from urllib.parse import urlsplit
    from mif_pipeline.qupath_runtime.catalogue import _project_path_from_uri

    entries = [entry for entry in history.get("images", [])
               if str(entry["entryID"]) == entry_id]
    if len(entries) != 1:
        raise ValueError("The source-history project lacks its exact retired image.")
    entry = entries[0]
    builder = entry.get("serverBuilder", {})
    metadata = entry.get("metadata", {})
    moving = metadata.get("mif_pipeline.image_role") == "moving_reference"
    if (builder.get("builderType") != "uri"
            or not isinstance(builder.get("providerClassName"), str)
            or (not moving and builder.get("providerClassName") != "org.mif.image.ProcessedImageServerBuilder")):
        raise ValueError("The retired image does not use its supported saved URI reader.")
    source_uri = urlsplit(builder.get("uri", ""))
    if source_uri.scheme != "file" or source_uri.netloc not in {"", "localhost"}:
        raise ValueError("The retired image must have a local file location.")
    original = _project_path_from_uri(builder["uri"]).resolve(strict=False)
    generation = entry.get("metadata", {}).get("mif_pipeline.source_image_sha256")
    destinations = set()
    for row in effective_locators:
        requirement = row["requirement"]
        identity = requirement["owning_identity"]
        if Path(requirement["original_path"]).resolve(strict=False) != original:
            continue
        if moving:
            if (requirement.get("role") != "paired_reference_source"
                    or identity.get("sample_id") != metadata.get("mif_pipeline.sample_id")
                    or row["validation_receipt"].get("sha256") != generation):
                continue
            raw_reference = identity.get("reference")
            if not isinstance(raw_reference, Mapping):
                continue
            if raw_reference.get("kind") == "paired_reference":
                from .batch_project import PairedMovingReferenceAuthority
                reference = PairedMovingReferenceAuthority.from_mapping(raw_reference)
                if (reference.source_sha256 != generation
                        or requirement.get("expected_sha256") != generation):
                    continue
            else:
                from .batch_manifest import PairedReferenceImage
                reference = PairedReferenceImage.from_mapping(raw_reference, field="retired paired image")
            if (reference.source_path.resolve(strict=False) != original
                    or reference.modality != metadata.get("mif_pipeline.reference_modality")):
                continue
        elif not (identity.get("artifact_kind") == "processed_image"
                and requirement.get("expected_sha256") == generation
                and identity.get("owning_identity", {}).get("image_generation_sha256") == generation):
            continue
        destinations.add(Path(row["effective_path"]).resolve(strict=False))
    if len(destinations) != 1:
        raise ValueError("The retired image needs one verified location for its saved generation.")
    replacement = next(iter(destinations))
    builder["uri"] = replacement.as_uri() + ("/" if source_uri.path.endswith("/") else "")
    history["images"] = [entry]
    history["uri"] = view_path.as_uri()
    return json.dumps(history, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def source_history_targets(payloads, project_path, *, effective_locators):
    """Restore native history and derive its relocated viewing projects at import.

    The capsule retains the original native metadata, projects and hierarchies.
    We check the metadata index against the active entries, publish those exact
    bytes, and generate one view per retired entry from authenticated image
    location receipts. Derived views are excluded from subsequent archives.
    """
    from mif_pipeline.qupath_runtime.project_content import _decode

    metadata_bytes = payloads.get("project/metadata.json")
    if metadata_bytes is None:
        if project_path.exists() or any(name.startswith("project/source-history/") for name in payloads):
            raise ValueError("The materialized project capsule lacks its native metadata.")
        return
    metadata = _decode(metadata_bytes)
    encoded_identity = metadata.get("mif_pipeline.project_identity_record_v1")
    if (not isinstance(encoded_identity, str)
            or _decode(encoded_identity.encode("utf-8")) != _decode(payloads["project/identity.json"])):
        raise ValueError("The native project metadata differs from the capsule project identity.")
    rows = source_history_entries(project_path, metadata_bytes=metadata_bytes)
    expected = {f"project/source-history/{row['mutation_request_sha256']}/{relative}"
                for row in rows for relative in ("project.qpproj", f"data/{row['project_entry_id']}/data.qpdata")}
    actual = {name for name in payloads if name.startswith("project/source-history/")}
    if actual != expected:
        raise ValueError("The source-history capsule inventory differs from the native metadata index.")
    # This exact-byte target also retains the project metadata that identifies
    # its archived source views; no separate history manifest is introduced.
    yield "source_project_history", project_path.parent / "data/metadata.json", metadata_bytes
    for row in rows:
        digest, entry_id = row["mutation_request_sha256"], row["project_entry_id"]
        prefix = f"project/source-history/{digest}/"
        history_bytes = payloads[prefix + "project.qpproj"]
        history = _decode(history_bytes)
        if sum(str(item["entryID"]) == entry_id for item in history.get("images", [])) != 1:
            raise ValueError("The source-history project does not contain its admitted previous image.")
        yield "source_project_history", project_path.parent / f"source-history-{digest}.qpproj", history_bytes
        relative = f"data/{entry_id}/data.qpdata"
        yield "source_entry_history", project_path.parent / relative, payloads[prefix + relative]
        view_path = project_path.parent / f"source-history-{digest}-view.qpproj"
        yield "source_project_history", view_path, _historical_image_view(
            history, entry_id, view_path, effective_locators,
        )
