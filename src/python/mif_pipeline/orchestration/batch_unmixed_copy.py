"""Confirmed component-image copying, without a reference-library workflow.

The accepted spectral state and channel semantics are the authority for a pixel- identical
copy. No spectral basis, acquisition normalization or AF subtraction is claimed. The
ordinary image publisher still owns atomic output/reuse checks.

The `ConfirmedUnmixedCopyCompletion` record carries `run_id`, `config_sha256`,
`target_image_sha256`, `checkpoint_sha256`, `final_authority_kind` and its remaining
declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
from .reference_unmixing import ReferenceUnmixingRequest
from .batch_preprocessing_support import file_sha256, publish_exact, unmixing_source_state
from .batch_preprocessing_wire import canonical_bytes, canonical_digest
from mif_pipeline.preprocessing.unmixing.validation import SpectralState
from mif_pipeline.preprocessing.unmixing.workflow import run_unmixing

# ==============================================================================
#                               Batch unmixed copy
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ConfirmedUnmixedCopyCompletion:
    run_id: str
    config_sha256: str
    target_image_sha256: str
    checkpoint_sha256: str
    final_authority_kind: str
    final_authority_id: str
    final_authority_sha256: str
    final_basis_sha256: None
    output_status: str
    output_path: str
    output_sha256: str
    provenance_path: str
    provenance_sha256: str
    regions_processed: int
    pyramid_levels: int
    warnings: tuple[str, ...]
    publication_warnings: tuple[str, ...]
    publication_request_sha256: str
    clipping_evidence_sha256: None
    content_sha256: str


def execute_confirmed_unmixed_copy(
    request: ReferenceUnmixingRequest,
    operation_root: Path,
    *,
    backend=None,
) -> ConfirmedUnmixedCopyCompletion:
    """Prepare the accepted component image and record its completed generation.

    We pass the reviewed source, channel names and image-state selection to the
    image writer. Its completed Zarr root identifies the selected image, and the
    adjacent processing record identifies its preparation. A matching existing
    receipt is reused; a changed source, review or selected result requires a
    corresponding revised preparation request.
    """
    if type(request) is not ReferenceUnmixingRequest:
        raise TypeError("The copy route requires its accepted typed request")
    binding = request.reviewed_source_binding()
    if binding is None:
        raise ValueError("The copy route requires the inspected source and accepted channel review")
    authority, ome = binding
    observed, source = unmixing_source_state(ome)
    declared = request.to_unmixing_config().spectral_state_override
    isolated = declared is SpectralState.ISOLATED
    if not isolated and (observed == "raw" or (observed != "unmixed" and declared is not SpectralState.UNMIXED)):
        raise ValueError("Only confirmed already-unmixed images can use this copy operation")
    if isolated:
        from mif_pipeline.io.metadata.measurement_band_copy import prepare_measurement_band_copy
        prepare_measurement_band_copy(ome, request.measurement_band_components)
    declaration = {
        "schema": "mif-confirmed-unmixed-copy-request/v1",
        "request_sha256": canonical_digest(request.to_mapping()),
        "source_sha256": authority.source_identity.file_sha256,
        "reviewed_ome_sha256": hashlib.sha256(request.reviewed_ome_xml.encode()).hexdigest(),
        "state_source": source if observed != "unknown" else "user_confirmation",
        "spectral_basis": None,
        "normalization": "not_applied",
    }
    if isolated:
        declaration.update(copy_reason="measurement_band_isolation",
            measurement_band_components=list(request.measurement_band_components))
    declaration_sha = canonical_digest(declaration)
    result = run_unmixing(
        request.input_path,
        request.output_path,
        None,
        request.to_unmixing_config(),
        backend=backend,
        reviewed_source_authority=authority,
        canonical_metadata_override=ome,
        replace_channel_names_with_biomarkers=request.replace_channel_names_with_biomarkers,
    )
    if result.status != "skipped_already_unmixed" or result.publication_request_sha256 is None:
        raise ValueError("The already-unmixed route did not publish its expected canonical copy")
    # We identify the completed image through its root generation metadata.
    # The image writer has already completed its chunks before returning here.
    output_sha, provenance_sha = file_sha256(result.output_path / "zarr.json"), file_sha256(
        result.provenance_path
    )
    receipt_path = Path(operation_root) / "confirmed-unmixed-copy.json"
    if receipt_path.exists():
        if receipt_path.is_symlink() or receipt_path.stat().st_size > 1024 * 1024:
            raise ValueError("The retained copy receipt is not a bounded regular file")
        payload = json.loads(receipt_path.read_bytes())
        digest = payload.pop("content_sha256", None)
        if digest != canonical_digest(payload) or set(payload) != set(
            ConfirmedUnmixedCopyCompletion.__dataclass_fields__
        ) - {"content_sha256"}:
            raise ValueError("The retained copy receipt has changed")
        if (
            payload["checkpoint_sha256"],
            payload["output_sha256"],
            payload["provenance_sha256"],
            payload["publication_request_sha256"],
        ) != (declaration_sha, output_sha, provenance_sha, result.publication_request_sha256):
            raise ValueError(
                "The current source, review or copied output differs from the retained receipt"
            )
        payload["warnings"] = tuple(payload["warnings"])
        payload["publication_warnings"] = tuple(payload["publication_warnings"])
        return ConfirmedUnmixedCopyCompletion(**payload, content_sha256=digest)
    values = dict(
        run_id=request.run_id,
        config_sha256=request.config_sha256,
        target_image_sha256=authority.source_identity.file_sha256,
        checkpoint_sha256=declaration_sha,
        final_authority_kind="already_unmixed_copy",
        final_authority_id="copy-" + declaration_sha[:24],
        final_authority_sha256=declaration_sha,
        final_basis_sha256=None,
        output_status=result.status,
        output_path=str(result.output_path),
        output_sha256=output_sha,
        provenance_path=str(result.provenance_path),
        provenance_sha256=provenance_sha,
        regions_processed=result.regions_processed,
        pyramid_levels=result.pyramid_levels,
        warnings=result.warnings,
        publication_warnings=result.publication_warnings,
        publication_request_sha256=result.publication_request_sha256,
        clipping_evidence_sha256=None,
    )
    completed = ConfirmedUnmixedCopyCompletion(**values, content_sha256=canonical_digest(values))
    publish_exact(receipt_path, canonical_bytes(asdict(completed)))
    return completed
