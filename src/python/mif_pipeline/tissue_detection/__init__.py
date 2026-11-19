"""QuPath-owned tissue-section generation contracts and lineage policy."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from mif_pipeline.pipeline_core.errors import ProjectPreparationError

from .generations import (
    DetectorValidationRecord,
    GenerationStatus,
    TissueDetectionQC,
    TissueSectionGeneration,
    validate_generation_manifest,
)
from .project import (
    TissueCandidate,
    TissueMatchMetrics,
    accept_tissue_candidate,
    edit_tissue_candidate,
    reconcile_tissue_candidates,
    reject_tissue_candidate,
    resolve_tissue_detection_prerequisites,
)
from .resolution import (
    NativeLevelReadAttestation,
    NativePyramidLevel,
    TissueResolutionSelection,
    native_pyramid_inventory_sha256,
    select_tissue_native_resolution,
)

__all__ = [
    "DetectorValidationRecord",
    "GenerationStatus",
    "NativeLevelReadAttestation",
    "NativePyramidLevel",
    "ProjectPreparationError",
    "TissueCandidate",
    "TissueDetectionQC",
    "TissueMatchMetrics",
    "TissueResolutionSelection",
    "TissueSectionGeneration",
    "accept_tissue_candidate",
    "edit_tissue_candidate",
    "native_pyramid_inventory_sha256",
    "reconcile_tissue_candidates",
    "reject_tissue_candidate",
    "resolve_tissue_detection_prerequisites",
    "select_tissue_native_resolution",
    "validate_generation_manifest",
]
