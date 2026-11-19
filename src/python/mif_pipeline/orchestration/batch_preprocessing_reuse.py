"""Strict owner loading and identity checks for reusable preprocessing inputs.

The `LoadedReusablePreprocessingInput` record carries `ref`, `payload`, `record`.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from mif_pipeline.preprocessing.inspection import (
    ReviewedPanelMapping,
    loads_reviewed_panel_mapping,
)
from mif_pipeline.preprocessing.unmixing import (
    FluorophoreReferenceLibrary,
    dumps_reference_artifact,
    loads_reference_artifact,
)

from .batch_artifacts import ArtifactOrigin, BatchArtifactRef
from .batch_preprocessing_contracts import reusable_input_identity_contract
from .batch_preprocessing_wire import canonical_digest

_MAX_REUSABLE_BYTES = 16 * 1024 * 1024


# ==============================================================================
#                           Batch preprocessing reuse
# ==============================================================================


@dataclass(frozen=True, slots=True)
class LoadedReusablePreprocessingInput:
    ref: BatchArtifactRef
    payload: bytes
    record: ReviewedPanelMapping | FluorophoreReferenceLibrary


def reviewed_panel_mapping_owner_identity(
    mapping: ReviewedPanelMapping,
) -> dict[str, object]:
    if type(mapping) is not ReviewedPanelMapping:
        raise TypeError("reviewed panel identity requires exact owner record")
    channels = [item.to_mapping() for item in mapping.channels]
    components = [item.component_name for item in (mapping.planned_components or mapping.channels)]
    fluorophores = [item.fluorophore for item in mapping.channels]
    biomarkers = [item.biomarker for item in (mapping.planned_components or mapping.channels)]
    return {
        "mapping_id": mapping.mapping_id,
        "content_sha256": mapping.content_sha256,
        "review_id": mapping.review_id,
        "review_decision": mapping.review_decision,
        "channel_selection_sha256": canonical_digest(
            list(mapping.source_band_ids) if mapping.planned_components else [
                {
                    "image_index": item.image_index,
                    "channel_index": item.channel_index,
                    "channel_id": item.channel_id,
                    "component_name": item.component_name,
                }
                for item in mapping.channels
            ]
        ),
        "component_inventory_sha256": canonical_digest(components),
        "fluorophore_inventory_sha256": canonical_digest(fluorophores),
        "biomarker_inventory_sha256": canonical_digest(biomarkers),
        "replace_channel_names_with_biomarkers": (mapping.replace_channel_names_with_biomarkers),
        "compatibility_signature_sha256": (mapping.compatibility_signature_sha256),
        "producer_identity_sha256": canonical_digest(
            {
                "producer_version": mapping.producer_version,
                "runtime_versions": dict(mapping.runtime_versions),
                "channels": channels,
            }
        ),
    }


def spectral_reference_library_owner_identity(
    library: FluorophoreReferenceLibrary,
) -> dict[str, object]:
    if type(library) is not FluorophoreReferenceLibrary:
        raise TypeError("spectral library identity requires accepted library")
    root = json.loads(dumps_reference_artifact(library))
    components = [item.component_identity for item in library.spectra]
    compatibility = canonical_digest(
        {
            "axis": root["axis"],
            "components": components,
            "structural_signature": root["structural_signature"],
            "quantitative_signature": root["quantitative_signature"],
        }
    )
    return {
        "artifact_id": library.artifact_id,
        "content_sha256": library.content_sha256,
        "axis_sha256": canonical_digest(root["axis"]),
        "component_order_sha256": canonical_digest(components),
        "structural_signature_sha256": canonical_digest(root["structural_signature"]),
        "quantitative_signature_sha256": canonical_digest(root["quantitative_signature"]),
        "qc_sha256": canonical_digest(root["qc"]),
        "review_sha256": canonical_digest(root["review"]),
        "software_versions_sha256": canonical_digest(root["software_versions"]),
        "policy_versions_sha256": canonical_digest(root["policy_versions"]),
        "compatibility_signature_sha256": compatibility,
    }


def _read_exact(ref: BatchArtifactRef) -> bytes:
    if ref.locator is None or ref.sha256 is None or ref.byte_count is None:
        raise ValueError("reusable preprocessing input must be file-backed")
    if ref.byte_count <= 0 or ref.byte_count > _MAX_REUSABLE_BYTES:
        raise ValueError("reusable preprocessing input size is unbounded")
    path = ref.locator
    try:
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise ValueError(f"could not read reusable preprocessing input: {path}") from exc
    before_identity = (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino)
    after_identity = (after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino)
    if before_identity != after_identity:
        raise ValueError("reusable preprocessing input changed while reading")
    if len(payload) != ref.byte_count or hashlib.sha256(payload).hexdigest() != ref.sha256:
        raise ValueError("reusable preprocessing input bytes differ from manifest")
    return payload


def load_reusable_preprocessing_input(
    ref: BatchArtifactRef,
) -> LoadedReusablePreprocessingInput:
    """Reload current bytes and prove exact owner identity before every target use."""

    if not isinstance(ref, BatchArtifactRef) or ref.origin is not ArtifactOrigin.MANIFEST_EXTERNAL:
        raise TypeError("reusable preprocessing input requires manifest authority")
    contract = reusable_input_identity_contract(ref.artifact_kind)
    if (ref.schema_name, ref.schema_version) != (
        contract.schema_name,
        contract.schema_version,
    ) or ref.owning_identity_contract_sha256 != contract.contract_sha256:
        raise ValueError("reusable preprocessing input owner contract differs")
    payload = _read_exact(ref)
    if ref.artifact_kind == "reviewed_panel_mapping":
        record = loads_reviewed_panel_mapping(payload)
        assert type(record) is ReviewedPanelMapping
        identity = reviewed_panel_mapping_owner_identity(record)
        expected_runtime = dict(record.runtime_versions)
        expected_producer = record.producer_version
    elif ref.artifact_kind == "spectral_reference_library":
        record = loads_reference_artifact(payload)
        if type(record) is not FluorophoreReferenceLibrary:
            raise ValueError("reusable spectral artifact is not a fluorophore library")
        identity = spectral_reference_library_owner_identity(record)
        expected_runtime = {item.name: item.version for item in record.software_versions}
        expected_producer = f"reference-library/v{FluorophoreReferenceLibrary.SCHEMA_VERSION}"
    else:  # pragma: no cover - closed by contract factory
        raise ValueError("unsupported reusable preprocessing input")
    if canonical_digest(ref.owning_identity) != canonical_digest(identity):
        raise ValueError("reusable preprocessing owner identity differs")
    if (
        ref.compatibility_signature_sha256 != identity["compatibility_signature_sha256"]
        or dict(ref.runtime_versions) != expected_runtime
        or ref.producer_version != expected_producer
    ):
        raise ValueError("reusable preprocessing compatibility or runtime differs")
    return LoadedReusablePreprocessingInput(ref=ref, payload=payload, record=record)


__all__ = [
    "LoadedReusablePreprocessingInput",
    "load_reusable_preprocessing_input",
    "reviewed_panel_mapping_owner_identity",
    "spectral_reference_library_owner_identity",
]
