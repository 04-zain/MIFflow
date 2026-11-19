"""Immutable analysis-domain identity, geometry, and selection contracts."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from .models import (
    ActiveAnalysisDomainAuthority,
    AnalysisDomainError,
    AnalysisDomainManifest,
    AnalysisDomainRevision,
    DomainGeometryEvidence,
    DomainOverlapEvidence,
    MAX_ANALYSIS_DOMAINS_PER_MANIFEST,
    MAX_ANALYSIS_DOMAIN_REVISIONS_PER_MANIFEST,
)
from .validation import (
    resolve_active_analysis_domain_authorities,
    validate_domain_manifest,
)

__all__ = [
    "ActiveAnalysisDomainAuthority",
    "AnalysisDomainError",
    "AnalysisDomainManifest",
    "AnalysisDomainRevision",
    "DomainGeometryEvidence",
    "DomainOverlapEvidence",
    "MAX_ANALYSIS_DOMAINS_PER_MANIFEST",
    "MAX_ANALYSIS_DOMAIN_REVISIONS_PER_MANIFEST",
    "resolve_active_analysis_domain_authorities",
    "validate_domain_manifest",
]
