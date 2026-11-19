"""Per-scale image features shared by conventional tissue classifier forms.

The same physical recipe is returned by the native inspector, edited directly
for remote execution, and retained with RF/SVM model settings.
"""
from .interactions import ControlKind, FormControl
from mif_pipeline.orchestration.batch_training.qupath_feature_settings import (
    PixelImageFeatures, pixel_feature_authoring_defaults,
)

FILTER_LABELS = {
    "gaussian": "Gaussian smoothing", "laplacian": "Laplacian",
    "weighted_std_dev": "Local standard deviation", "gradient_magnitude": "Gradient magnitude",
    "hessian_determinant": "Hessian determinant",
    "hessian_eigenvalue_max": "Hessian: largest eigenvalue",
    "hessian_eigenvalue_min": "Hessian: smallest eigenvalue",
    "structure_tensor_eigenvalue_max": "Structure tensor: largest eigenvalue",
    "structure_tensor_eigenvalue_min": "Structure tensor: smallest eigenvalue",
    "structure_tensor_coherence": "Structure tensor coherence",
}


def image_feature_controls(prefix: str) -> tuple[FormControl, ...]:
    """Bind one physical recipe to the native summary or remote scale editor."""
    return (FormControl(prefix + "_image_features", "Image features", ControlKind.JSON_OBJECT,
        value=pixel_feature_authoring_defaults()["image_features"],
        help_text="New scales copy the selected scale’s filters."),)


def image_features_from_values(values, prefix: str) -> dict[str, object]:
    """Validate the entered recipe, omitting scale rows without selected filters."""
    if str(values.get(prefix + "_model_method", "")).startswith("kronos_"):
        return PixelImageFeatures(True, ()).to_mapping()
    raw = values.get(prefix + "_image_features")
    try:
        if not isinstance(raw, dict) or set(raw) != {"include_intensity", "scales"}:
            raise ValueError("Choose image features in Image preparation.")
        groups = []
        for row in raw["scales"]:
            if row["filters"]:
                try:
                    sigma = float(row["sigma_um"])
                except (TypeError, ValueError):
                    raise ValueError("Enter a positive Gaussian σ for every scale with selected filters.") from None
                groups.append(dict(sigma_um=sigma, filters=row["filters"]))
        return PixelImageFeatures(raw["include_intensity"], groups).to_mapping()
    except (TypeError, KeyError, ValueError) as exc:
        message = str(exc).split(": ", 1)[-1] if isinstance(exc, ValueError) else "Choose a Gaussian σ and filters for each scale."
        raise ValueError(f"{prefix}_image_features: {message}") from exc


def image_feature_summary(recipe) -> str:
    """Describe the actual filters assigned to each physical scale."""
    lines = ["Channel intensity"] if recipe["include_intensity"] else []
    lines.extend(f"σ {row['sigma_um']:.6g} µm: " + ", ".join(FILTER_LABELS[key] for key in row["filters"])
                 for row in recipe["scales"])
    return "\n".join(lines)
