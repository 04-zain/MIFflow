"""Translate the current Samples draft to cluster paths without editing identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from .manifest_draft import ManifestDraft, _sample_mapping
from .mounted_images import MountedImageAccess
from mif_pipeline.io.aligned_collection import is_aligned_source_collection, load_aligned_source_collection

# ==============================================================================
#                           Cluster sample projection
# ==============================================================================


def cluster_samples(draft, mounted):
    if not isinstance(draft, ManifestDraft):
        raise ValueError("Add and save the images on Samples before preparing cluster processing.")
    if not isinstance(mounted, MountedImageAccess):
        raise ValueError(
            "Configure the mounted image folder so the paths on Samples can be matched to cluster storage."
        )
    rows = []
    for sample in draft.samples:
        if not sample.enabled:
            continue
        if not sample.sample_id.strip() or not sample.subject_id.strip():
            raise ValueError(
                "Complete the sample name and patient or animal identifier on Samples."
            )
        row = _sample_mapping(sample)
        row["validation_domain_id"] = sample.validation_domain_id or draft.validation_domain_id
        if is_aligned_source_collection(sample.source_mif_path):
            collection = load_aligned_source_collection(sample.source_mif_path)
            try:
                mapped = tuple(mounted.cluster_path(str(member.path)) for member in collection.members)
            except ValueError:
                # A reopened cluster analysis already owns its remote descriptor.
                # Verify that every member belongs to this configured cluster mount.
                for member in collection.members:
                    mounted.local_path(str(member.path))
                row["source_mif_path"] = mounted.cluster_path(str(sample.source_mif_path))
            else:
                request = collection.to_mapping()
                request["schema_name"] = "mif-aligned-source-collection-request"
                for member, path in zip(request["members"], mapped, strict=True):
                    member["path"] = path
                    del member["identity"]
                row["source_collection"] = request
                # The setup endpoint replaces this locator with its owned descriptor.
                row["source_mif_path"] = mapped[0]
                for field in ("source_image_index", "source_series_index", "source_file_sha256"):
                    row.pop(field, None)
        else:
            row["source_mif_path"] = mounted.cluster_path(str(sample.source_mif_path))
        reference = row.get("paired_reference_image")
        if reference is not None and sample.moving_reference_path is not None:
            reference["source_path"] = mounted.cluster_path(str(sample.moving_reference_path))
        rows.append(row)
    if not rows:
        raise ValueError(
            "Include at least one image on Samples before preparing cluster processing."
        )
    return rows
