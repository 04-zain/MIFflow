"""Canonical input bindings for recoverable reference-result operations.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from .interactions import SelectedIdentifier
from .reference_qc import SpectrumMatrixQcPolicy
from .reference_service_composition import ReferenceLibraryPublicationMetadata
from .typed_checksum import typed_checksum_sha256

# ==============================================================================
#                      Reference service operation bindings
# ==============================================================================


def _literal_text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty literal text")
    return value


def _extraction_operation_binding(
    *,
    source_checkpoint_sha256: str,
    inventory_sha256: str,
    dispositions: tuple[SelectedIdentifier, ...],
    reviewer_id: str,
) -> tuple[str, str]:
    return (
        "reference_extraction_operation_v1",
        typed_checksum_sha256(
            {
                "semantic": "reference_extraction_operation_v1",
                "source_checkpoint_sha256": source_checkpoint_sha256,
                "inventory_sha256": inventory_sha256,
                "dispositions": [item.to_mapping() for item in dispositions],
                "reviewer_id": _literal_text(reviewer_id, "reviewer_id"),
            }
        ),
    )


def _consensus_operation_binding(
    *,
    source_checkpoint_sha256: str,
    candidate_sha256: str,
    qc_policy: SpectrumMatrixQcPolicy,
    reviewer_id: str,
    reviewed_at: str,
    publication: ReferenceLibraryPublicationMetadata | None,
) -> tuple[str, str]:
    publication_mapping = None
    if publication is not None:
        publication_mapping = {
            "artifact_id": publication.artifact_id,
            "created_at": publication.created_at,
            "reviewed_at": publication.reviewed_at,
            "software_versions": [list(item) for item in publication.software_versions],
            "policy_versions": [list(item) for item in publication.policy_versions],
            "creation_reason": publication.creation_reason,
        }
    return (
        "reference_consensus_operation_v1",
        typed_checksum_sha256(
            {
                "semantic": "reference_consensus_operation_v1",
                "source_checkpoint_sha256": source_checkpoint_sha256,
                "candidate_sha256": candidate_sha256,
                "qc_policy_authority_sha256": qc_policy.authority_sha256,
                "qc_policy_content_sha256": qc_policy.content_sha256,
                "reviewer_id": _literal_text(reviewer_id, "reviewer_id"),
                "reviewed_at": _literal_text(reviewed_at, "reviewed_at"),
                "publication": publication_mapping,
            }
        ),
    )


def _reconstruct_qc_policy(
    policy: SpectrumMatrixQcPolicy,
) -> SpectrumMatrixQcPolicy:
    if type(policy) is not SpectrumMatrixQcPolicy:
        raise TypeError("QC policy must be exact SpectrumMatrixQcPolicy")
    rebuilt = SpectrumMatrixQcPolicy(
        policy_version=policy.policy_version,
        svd_absolute_tolerance=policy.svd_absolute_tolerance,
        svd_relative_tolerance=policy.svd_relative_tolerance,
        cosine_review_threshold=policy.cosine_review_threshold,
        cosine_fail_threshold=policy.cosine_fail_threshold,
        condition_review_threshold=policy.condition_review_threshold,
        condition_fail_threshold=policy.condition_fail_threshold,
        normalization_tolerance=policy.normalization_tolerance,
        authority_sha256=policy.authority_sha256,
    )
    if rebuilt != policy:
        raise ValueError("QC policy authority is stale")
    return rebuilt


__all__ = []
