"""Canonical JSON codec for aggregate cluster-review evidence."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import json

from mif_pipeline.orchestration.batch_artifacts import BatchArtifactError

from ._common import BatchPhenotypingError, canonical_bytes, integer
from .review_records import BatchClusterReviewEvidence

# ==============================================================================
#                                  Review codec
# ==============================================================================


def dumps_batch_cluster_review_evidence(value: BatchClusterReviewEvidence) -> bytes:
    if type(value) is not BatchClusterReviewEvidence:
        raise BatchPhenotypingError("cluster review evidence must be an exact owner record")
    return canonical_bytes(value.to_mapping())


def loads_batch_cluster_review_evidence(
    payload: bytes,
    *,
    maximum_byte_count: int = 64 * 1024 * 1024,
) -> BatchClusterReviewEvidence:
    """Decode one closed canonical summary under an explicit byte ceiling."""

    maximum = integer(maximum_byte_count, "maximum_byte_count", positive=True)
    if type(payload) is not bytes or not payload or len(payload) > maximum:
        raise BatchPhenotypingError("cluster review evidence bytes are invalid")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchPhenotypingError("cluster review evidence is not JSON") from exc
    if canonical_bytes(decoded) != payload:
        raise BatchPhenotypingError("cluster review evidence JSON is not canonical")
    try:
        return BatchClusterReviewEvidence.from_mapping(decoded)
    except BatchPhenotypingError:
        raise
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("cluster review evidence content is invalid") from exc


__all__ = [
    "dumps_batch_cluster_review_evidence",
    "loads_batch_cluster_review_evidence",
]
