"""Shared QuPath pixel/object feature settings for native classifiers.

Training records keep subject roles, reference evidence, model settings and application
scope linked. Their validators and native adapters are called by the shared training service
before a fit, evaluation or accepted publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from typing import Literal

from ._shared import BatchTrainingError, _array, _closed_mapping
from .native_projection import QuPathOutputLabels

_NORMALIZATIONS = frozenset({"none", "mean_variance", "min_max"})
_BOUNDARY_STRATEGIES = frozenset({"skip", "classify"})
_JAVA_INT_MAX = 2_147_483_647
_PIXEL_LABEL_MAX = 255
PIXEL_IMAGE_FILTERS = (
    "gaussian", "laplacian", "weighted_std_dev", "gradient_magnitude",
    "hessian_determinant", "hessian_eigenvalue_max", "hessian_eigenvalue_min",
    "structure_tensor_eigenvalue_max", "structure_tensor_eigenvalue_min",
    "structure_tensor_coherence",
)

PIXEL_FEATURE_FIELDS = frozenset(
    {
        "resolution_um_per_pixel",
        "image_features",
        "maximum_training_samples",
        "reweight_samples",
        "normalization",
        "pca_retained_variance",
        "pca_normalize",
        "boundary_strategy",
        "boundary_thickness_px",
        "output_labels",
    }
)
OBJECT_FEATURE_FIELDS = frozenset(
    {
        "object_filter",
        "training_annotations",
        "output_classes",
        "normalization",
        "pca_retained_variance",
        "multiclass",
        "output_labels",
    }
)


# ==============================================================================
#                            Qupath feature settings
# ==============================================================================

@dataclass(frozen=True, slots=True)
class PixelFeatureScale:
    """Filters calculated together at one physical Gaussian sigma in micrometres."""

    sigma_um: float
    filters: tuple[str, ...]

    def __post_init__(self) -> None:
        sigma = _finite_number(self.sigma_um, "image_features.scales.sigma_um", minimum=0.0, positive=True)
        filters = tuple(_array(self.filters, "image_features.scales.filters"))
        if not filters or any(type(item) is not str or item not in PIXEL_IMAGE_FILTERS for item in filters):
            raise BatchTrainingError("image_features.scales.filters: Select supported filters for each scale")
        if len(set(filters)) != len(filters):
            raise BatchTrainingError("image_features.scales.filters must not repeat a filter within a scale")
        object.__setattr__(self, "sigma_um", sigma)
        object.__setattr__(self, "filters", filters)

    @classmethod
    def from_mapping(cls, value: object) -> "PixelFeatureScale":
        fields = _closed_mapping(value, fields=frozenset({"sigma_um", "filters"}), field="image_features.scales")
        return cls(**fields)

    def to_mapping(self) -> dict[str, object]:
        return {"sigma_um": self.sigma_um, "filters": list(self.filters)}


@dataclass(frozen=True, slots=True)
class PixelImageFeatures:
    """Select raw channels and the filters belonging to each physical scale.

    Scale and filter order are retained in the recipe. QuPath defines the output
    column order within each scale's operation; raw channels precede those groups.
    No feature images are stored alongside the source image.
    """

    include_intensity: bool
    scales: tuple[PixelFeatureScale, ...]

    def __post_init__(self) -> None:
        _boolean(self.include_intensity, "image_features.include_intensity")
        scales = tuple(item if type(item) is PixelFeatureScale else PixelFeatureScale.from_mapping(item)
                       for item in _array(self.scales, "image_features.scales"))
        if len({item.sigma_um for item in scales}) != len(scales):
            raise BatchTrainingError("image_features.scales: Use a different Gaussian sigma for each scale")
        if not self.include_intensity and not scales:
            raise BatchTrainingError("image_features: Include channel intensity or select at least one image filter")
        object.__setattr__(self, "scales", scales)

    @classmethod
    def from_mapping(cls, value: object) -> "PixelImageFeatures":
        if not isinstance(value, Mapping) or set(value) != {"include_intensity", "scales"}:
            raise BatchTrainingError("image_features: Choose image features in Image preparation; the recipe must contain include_intensity and per-scale filters")
        return cls(**value)

    def to_mapping(self) -> dict[str, object]:
        return {"include_intensity": self.include_intensity,
                "scales": [item.to_mapping() for item in self.scales]}


def configured_image_features(configuration: Mapping[str, object]) -> PixelImageFeatures:
    """Read the common image recipe from fixed settings or a parameter search.

    Search candidates vary estimator parameters, while sharing image preparation.
    KRONOS retains the raw-only native recipe because its encoder supplies features.
    """
    if not isinstance(configuration, Mapping):
        raise BatchTrainingError("Image feature settings require a model configuration")
    schema = configuration.get("configuration_schema")
    if schema == "qupath-grouped-cross-validation/v1":
        recipes = tuple(configured_image_features(row) for row in _array(configuration.get("candidates"), "candidates"))
        if not recipes or any(recipe != recipes[0] for recipe in recipes[1:]):
            raise BatchTrainingError("Parameter search candidates must use the same image_features")
        return recipes[0]
    if schema in {"qupath-kronos-rtrees/v2", "qupath-kronos-svm/v2"}:
        return configured_image_features(configuration.get("native_configuration"))
    return PixelImageFeatures.from_mapping(configuration.get("image_features"))


def pixel_feature_authoring_defaults() -> dict[str, object]:
    """Explicit non-geometric defaults; resolution and biological labels remain chosen."""
    from mif_pipeline._defaults import default_section

    return default_section("classifier.features")


def _integer(value: object, field: str, *, minimum: int, maximum: int = _JAVA_INT_MAX) -> int:
    if type(value) is not int:
        raise BatchTrainingError(f"{field} must be an integer")
    if value < minimum:
        raise BatchTrainingError(f"{field} must be at least {minimum}")
    if value > maximum:
        raise BatchTrainingError(f"{field} must be at most {maximum}")
    return value


def _finite_number(value: object, field: str, *, minimum: float, positive: bool) -> float:
    if type(value) not in (int, float):
        raise BatchTrainingError(f"{field} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise BatchTrainingError(f"{field} must be a finite number") from exc
    if not math.isfinite(result):
        raise BatchTrainingError(f"{field} must be a finite number")
    if result < minimum or (positive and result == minimum):
        comparison = "greater than" if positive else "at least"
        raise BatchTrainingError(f"{field} must be {comparison} {minimum}")
    return result


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise BatchTrainingError(f"{field} must be a boolean")
    return value


def _normalization(value: object) -> Literal["none", "mean_variance", "min_max"]:
    if not isinstance(value, str) or value not in _NORMALIZATIONS:
        raise BatchTrainingError("normalization is not a supported value")
    return value  # type: ignore[return-value]


def _boundary_strategy(value: object) -> Literal["skip", "classify"]:
    if value == "derive":
        raise BatchTrainingError("boundary_strategy derive is unsupported for prototype v1")
    if not isinstance(value, str) or value not in _BOUNDARY_STRATEGIES:
        raise BatchTrainingError("boundary_strategy is not a supported value")
    return value  # type: ignore[return-value]


def _configuration_mapping(value: object, *, family: str, model_field: str) -> Mapping[str, object]:
    if family == "pixel":
        if not isinstance(value, Mapping):
            raise BatchTrainingError(f"qupath_pixel_{model_field}_configuration must be an object")
        fields = PIXEL_FEATURE_FIELDS | {"configuration_schema", model_field}
        if value.get("boundary_strategy") == "classify":
            fields |= {"boundary_class_id"}
    elif family == "object":
        fields = OBJECT_FEATURE_FIELDS | {"configuration_schema", model_field}
        if isinstance(value, Mapping) and "pca_normalize" in value:
            fields |= {"pca_normalize"}
    else:
        raise BatchTrainingError("native feature family is unsupported")
    return _closed_mapping(
        value,
        fields=frozenset(fields),
        field=f"qupath_{family}_{model_field}_configuration",
    )


def _pixel_features(value: object) -> dict[str, object]:
    resolution = _finite_number(
        value.resolution_um_per_pixel, "resolution_um_per_pixel", minimum=0.0, positive=True
    )
    maximum = _integer(value.maximum_training_samples, "maximum_training_samples", minimum=0)
    reweight = _boolean(value.reweight_samples, "reweight_samples")
    normalization = _normalization(value.normalization)
    variance = _finite_number(
        value.pca_retained_variance, "pca_retained_variance", minimum=0.0, positive=False
    )
    if variance > 1.0:
        raise BatchTrainingError("pca_retained_variance must be at most 1.0")
    pca_normalize = _boolean(value.pca_normalize, "pca_normalize")
    if variance == 0.0 and pca_normalize:
        raise BatchTrainingError("pca_normalize must be false when pca_retained_variance is 0")
    strategy = _boundary_strategy(value.boundary_strategy)
    thickness = _finite_number(
        value.boundary_thickness_px, "boundary_thickness_px", minimum=0.0, positive=False
    )
    if strategy == "skip" and thickness != 0.0:
        raise BatchTrainingError(
            "boundary_thickness_px must be exactly 0 for boundary_strategy skip"
        )
    if strategy == "classify" and thickness <= 0.0:
        raise BatchTrainingError("boundary_thickness_px must be greater than 0 for classify")
    boundary_id = value.boundary_class_id
    if strategy == "classify":
        boundary_id = _integer(boundary_id, "boundary_class_id", minimum=0)
    elif boundary_id is not None:
        raise BatchTrainingError(
            "boundary_class_id is present only when boundary_strategy is classify"
        )
    labels = value.output_labels
    if type(labels) is not QuPathOutputLabels:
        raise BatchTrainingError("output_labels must be exact QuPathOutputLabels")
    if any(item.qupath_label > _PIXEL_LABEL_MAX for item in labels.ordered_labels):
        raise BatchTrainingError(f"pixel output labels must be at most {_PIXEL_LABEL_MAX}")
    if boundary_id is not None and boundary_id not in {
        item.qupath_label for item in labels.ordered_labels
    }:
        raise BatchTrainingError("boundary_class_id is absent from the native output label map")
    return {
        "resolution_um_per_pixel": resolution,
        "image_features": (value.image_features if type(value.image_features) is PixelImageFeatures
                           else PixelImageFeatures.from_mapping(value.image_features)),
        "maximum_training_samples": maximum,
        "reweight_samples": reweight,
        "normalization": normalization,
        "pca_retained_variance": variance,
        "pca_normalize": pca_normalize,
        "boundary_strategy": strategy,
        "boundary_thickness_px": thickness,
        "boundary_class_id": boundary_id,
        "output_labels": labels,
    }


def _object_features(value: object) -> dict[str, object]:
    if value.object_filter != "cells":
        raise BatchTrainingError("object_filter must be cells")
    if value.training_annotations != "points":
        raise BatchTrainingError("training_annotations must be points")
    if value.output_classes != "selected":
        raise BatchTrainingError("output_classes must be selected")
    normalization = _normalization(value.normalization)
    variance = _finite_number(
        value.pca_retained_variance, "pca_retained_variance", minimum=0.0, positive=False
    )
    if variance > 1.0:
        raise BatchTrainingError("pca_retained_variance must be at most 1.0")
    _boolean(value.pca_normalize, "pca_normalize")
    multiclass = _boolean(value.multiclass, "multiclass")
    if multiclass is not False:
        raise BatchTrainingError("multiclass must be the fixed literal false")
    labels = value.output_labels
    if type(labels) is not QuPathOutputLabels:
        raise BatchTrainingError("output_labels must be exact QuPathOutputLabels")
    if len(labels.ordered_labels) < 2:
        raise BatchTrainingError("object classifiers require at least two output classes")
    if any(item.qupath_label != index for index, item in enumerate(labels.ordered_labels)):
        raise BatchTrainingError("object output labels must equal their zero-based tuple position")
    return {
        "normalization": normalization,
        "pca_retained_variance": variance,
        "multiclass": multiclass,
        "output_labels": labels,
    }


def _pixel_feature_mapping(value: object) -> dict[str, object]:
    mapping = {
        name: getattr(value, name) for name in PIXEL_FEATURE_FIELDS if name != "output_labels"
    }
    mapping["output_labels"] = value.output_labels.to_mapping()
    mapping["image_features"] = value.image_features.to_mapping()
    if value.boundary_strategy == "classify":
        mapping["boundary_class_id"] = value.boundary_class_id
    return mapping


def _object_feature_mapping(value: object) -> dict[str, object]:
    mapping = {
        name: getattr(value, name) for name in OBJECT_FEATURE_FIELDS if name != "output_labels"
    }
    # Existing cell models always whitened their components. Preserve their saved identity.
    if not value.pca_normalize and value.pca_retained_variance > 0:
        mapping["pca_normalize"] = False
    mapping["output_labels"] = value.output_labels.to_mapping()
    return mapping
