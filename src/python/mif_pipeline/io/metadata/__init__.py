"""Canonical OME metadata, biomarker, and spectral contracts.

Metadata values are checked against the selected analytical image and represented through
canonical OME fields or explicitly named annotations. The returned records preserve the
source of reviewed values for downstream processing."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from .biomarkers import (
    BIOMARKER_ASSIGNMENT_FIELDS,
    BiomarkerAssignmentRow,
    apply_biomarker_assignment_rows,
    read_biomarker_assignment_csv,
    resolve_biomarker_assignment_rows,
    render_biomarker_assignment_csv,
)
from .canonical import (
    CHANNEL_METADATA_NAMESPACE,
    COMPONENT_IDENTITY_NAMESPACE,
    INGESTION_PROVENANCE_NAMESPACE,
    ORIGINAL_METADATA_NAMESPACE,
    UNMIXING_INPUT_NAMESPACE,
    BiomarkerAssignment,
    ChannelMetadata,
    ChannelMetadataIndex,
    ComponentIdentityInventory,
    ComponentIdentityMode,
    OffsetAuthority,
    UnmixingInputMetadata,
    apply_component_identity_inventory,
    canonicalize_ome,
    resolve_channel_metadata,
    resolve_channel_metadata_index,
    resolve_component_identity_inventory,
    resolve_unmixing_input_metadata,
    validate_offset_specification,
)
from .spectral import (
    SPECTRAL_PROCESSING_NAMESPACE,
    SPECTRAL_PROVENANCE_NAMESPACE,
    ProcessingProvenance,
    ProvenanceApplicability,
    ResolvedSpectralState,
    SpectralLibraryMetadata,
    build_unmixed_ome,
    build_metadata_copy_ome,
    loads_processing_provenance,
    render_processing_provenance,
    resolve_processing_provenance,
    resolve_spectral_state,
    validate_processing_provenance,
)
from .resolved_basis import ResolvedBasisAuthorityProvenance

__all__ = [
    "BIOMARKER_ASSIGNMENT_FIELDS",
    "BiomarkerAssignmentRow",
    "apply_biomarker_assignment_rows",
    "read_biomarker_assignment_csv",
    "resolve_biomarker_assignment_rows",
    "render_biomarker_assignment_csv",
    "CHANNEL_METADATA_NAMESPACE",
    "COMPONENT_IDENTITY_NAMESPACE",
    "INGESTION_PROVENANCE_NAMESPACE",
    "ORIGINAL_METADATA_NAMESPACE",
    "UNMIXING_INPUT_NAMESPACE",
    "BiomarkerAssignment",
    "ChannelMetadata",
    "ChannelMetadataIndex",
    "ComponentIdentityInventory",
    "ComponentIdentityMode",
    "OffsetAuthority",
    "UnmixingInputMetadata",
    "apply_component_identity_inventory",
    "canonicalize_ome",
    "resolve_channel_metadata",
    "resolve_channel_metadata_index",
    "resolve_component_identity_inventory",
    "resolve_unmixing_input_metadata",
    "validate_offset_specification",
    "SPECTRAL_PROCESSING_NAMESPACE",
    "SPECTRAL_PROVENANCE_NAMESPACE",
    "SpectralLibraryMetadata",
    "ResolvedSpectralState",
    "ProcessingProvenance",
    "ProvenanceApplicability",
    "build_unmixed_ome",
    "build_metadata_copy_ome",
    "loads_processing_provenance",
    "render_processing_provenance",
    "resolve_processing_provenance",
    "resolve_spectral_state",
    "validate_processing_provenance",
    "ResolvedBasisAuthorityProvenance",
]
