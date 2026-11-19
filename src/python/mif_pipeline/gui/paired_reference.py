"""Explicit per-sample paired-image use, without changing biological inputs."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from dataclasses import replace
from .analysis_settings import copy_settings
from mif_pipeline.pipeline_core.config import RegistrationConfig

MODES = (
    ("Do not use the paired image", "none"),
    ("Compare images side by side", "side_by_side"),
    ("Align with mIF and use an overlay", "warpy"),
)

# ==============================================================================
#                                Paired reference
# ==============================================================================


def paired_reference_mode(sample):
    return sample.overrides.get("registration", {}).get("mode", "none") if sample else "none"


def configure_paired_reference(sample, mode):
    if mode not in {token for label, token in MODES}:
        raise ValueError("Choose how to use the paired image")
    if mode != "none" and (
        sample.moving_reference_path is None
        or not sample.moving_reference_id
        or not sample.moving_reference_modality
    ):
        raise ValueError(
            "Select the paired image, its name and image type before enabling alignment"
        )
    settings = copy_settings(sample.overrides)
    existing = settings.get("registration", {})
    value = {"mode": mode, "moving_assistance_policy": "none", "moving_assistance_scopes": []}
    if mode != "none":
        value.update(
            moving_image=str(sample.moving_reference_path),
            moving_image_id=sample.moving_reference_id,
            moving_modality=sample.moving_reference_modality,
            moving_image_metadata=copy_settings(sample.moving_reference_source_metadata),
        )
        for key in ("registration_qc_policy", "transfer_qc_policy"):
            if key in existing:
                value[key] = existing[key]
    settings["registration"] = RegistrationConfig.from_mapping(
        value, field="registration"
    ).to_mapping()
    return replace(sample, overrides=settings)


def refresh_paired_reference(sample):
    mode = paired_reference_mode(sample)
    if (
        "registration" not in sample.overrides
        and sample.moving_reference_path is not None
        and sample.moving_reference_id
        and sample.moving_reference_modality
    ):
        return configure_paired_reference(sample, "warpy")
    if mode not in {"warpy", "side_by_side"}:
        return sample
    if sample.moving_reference_path is None:
        return configure_paired_reference(sample, "none")
    if not sample.moving_reference_id or not sample.moving_reference_modality:
        return sample
    return configure_paired_reference(sample, mode)
