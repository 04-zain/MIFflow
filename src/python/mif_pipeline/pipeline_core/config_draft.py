"""Unresolved analysis settings shared by desktop and headless preparation.

This is an editable draft. The existing AnalysisConfig and manifest validators remain the
only configuration acceptance boundary."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from .config import SCHEMA_VERSION as ANALYSIS_CONFIG_SCHEMA_VERSION

# ==============================================================================
#                                  Config draft
# ==============================================================================


def unresolved_analysis_settings(batch_id, output_root, slide_id=None):
    output = output_root / "analysis"
    return {
        "schema_version": ANALYSIS_CONFIG_SCHEMA_VERSION,
        "slide_id": slide_id or batch_id,
        "input_image": str(output_root / "preprocessing" / "analysis-ready.ome.zarr"),
        "provenance": str(output_root / "preprocessing" / "analysis-ready.provenance.json"),
        "output_dir": str(output),
        "channels": None,
        "primary_classes": None,
        "primary_classifier": None,
        "secondary_structures": None,
        "tissue_detection": None,
        "analysis_domain": {
            "selection_policy": "prompt",
            "stage_version": "analysis-domain/v1",
            "review_policy": "always",
            "saved_selection_path": None,
        },
        "registration": {"mode": "none"},
        "segmentation": None,
        "phenotyping": {
            "mode_policy": {"schema_version": "phenotyping-mode-policy/v1", "mode": None},
            "phenotype_classes": [],
            "known_marker_rules": {},
            "composite_rules": [],
        },
        "clustering": {
            "enabled": False,
            "mode": None,
            "feature_specs": [],
            "k": None,
            "resolution": None,
            "seed": None,
            "exclude_channel_roles": ["nuclear", "autofluorescence", "dapi"],
        },
        "semantic": {"profile_references": [], "roi_review_policy": "always"},
    }
