"""Validate the native SVM parameters and feature settings recorded for QuPath fitting.

The configuration distinguishes the kernel, C, applicable gamma and termination criteria.
Its pixel feature settings and label mapping are checked before native execution. The saved
model reuses the same fitted feature normalization during prediction."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from typing import ClassVar, Literal

from ._shared import BatchTrainingError, _array, _closed_mapping
from .native_projection import QuPathOutputLabels
from .qupath_feature_settings import (
    PixelImageFeatures,
    _configuration_mapping,
    _object_feature_mapping,
    _object_features,
    _pixel_feature_mapping,
    _pixel_features,
)

_JAVA_INT_MAX = 2_147_483_647


# ==============================================================================
#                                   Qupath svm
# ==============================================================================


def _positive_finite(value: object, field: str) -> float:
    if type(value) not in (int, float):
        raise BatchTrainingError(f"{field} must be a positive finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise BatchTrainingError(f"{field} must be a positive finite number") from exc
    if not math.isfinite(result) or result <= 0:
        raise BatchTrainingError(f"{field} must be a positive finite number")
    return result


def _java_integer(value: object, field: str, *, minimum: int) -> int:
    if type(value) is not int or not minimum <= value <= _JAVA_INT_MAX:
        raise BatchTrainingError(f"{field} must be an integer from {minimum} to {_JAVA_INT_MAX}")
    return value


@dataclass(frozen=True, slots=True)
class QuPathSvmParameters:
    """Complete accepted controls for OpenCV C_SVC training."""

    model_type: Literal["C_SVC"]
    kernel: Literal["LINEAR", "RBF"]
    c: float
    gamma: float | None
    max_iterations: int
    termination_epsilon: float

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"model_type", "kernel", "c", "gamma", "max_iterations", "termination_epsilon"}
    )

    def __post_init__(self) -> None:
        if self.model_type != "C_SVC":
            raise BatchTrainingError("model_type must be C_SVC")
        if self.kernel not in ("LINEAR", "RBF"):
            raise BatchTrainingError("kernel must be LINEAR or RBF")
        object.__setattr__(self, "c", _positive_finite(self.c, "c"))
        if self.kernel == "LINEAR":
            if self.gamma is not None:
                raise BatchTrainingError("gamma must be null for LINEAR")
        else:
            object.__setattr__(self, "gamma", _positive_finite(self.gamma, "gamma"))
        object.__setattr__(
            self,
            "max_iterations",
            _java_integer(self.max_iterations, "max_iterations", minimum=1),
        )
        object.__setattr__(
            self,
            "termination_epsilon",
            _positive_finite(self.termination_epsilon, "termination_epsilon"),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "QuPathSvmParameters":
        mapping = _closed_mapping(value, fields=cls._FIELDS, field="qupath_svm")
        return cls(**{field: mapping[field] for field in cls._FIELDS})  # type: ignore[arg-type]

    @classmethod
    def authoring_defaults(cls) -> "QuPathSvmParameters":
        """Complete linear values for authoring only; never execution defaults."""
        from mif_pipeline._defaults import default_section

        return cls.from_mapping(default_section("classifier.svm"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "model_type": self.model_type,
            "kernel": self.kernel,
            "c": self.c,
            "gamma": self.gamma,
            "max_iterations": self.max_iterations,
            "termination_epsilon": self.termination_epsilon,
        }


@dataclass(frozen=True, slots=True)
class QuPathNativeSvmExecutionProjection:
    """Exact raw SVM fit route, seed, and accepted parameters."""

    schema: Literal["qupath-native-svm-execution/v1"]
    native_engine: Literal["qupath_bundled_opencv"]
    native_model_family: Literal["opencv_ml.SVM"]
    training_path: Literal["raw_svm_train_then_qupath_public_wrap"]
    seed_behavior: Literal["set_rng_seed_immediately_before_train"]
    random_seed: int
    parameters: QuPathSvmParameters
    termination_flags: tuple[Literal["MAX_ITER"], Literal["EPS"]]

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema",
            "native_engine",
            "native_model_family",
            "training_path",
            "seed_behavior",
            "random_seed",
            "parameters",
            "termination_flags",
        }
    )

    def __post_init__(self) -> None:
        fixed = {
            "schema": "qupath-native-svm-execution/v1",
            "native_engine": "qupath_bundled_opencv",
            "native_model_family": "opencv_ml.SVM",
            "training_path": "raw_svm_train_then_qupath_public_wrap",
            "seed_behavior": "set_rng_seed_immediately_before_train",
        }
        for field, expected in fixed.items():
            if getattr(self, field) != expected:
                raise BatchTrainingError(f"{field} must be {expected}")
        object.__setattr__(
            self, "random_seed", _java_integer(self.random_seed, "random_seed", minimum=0)
        )
        if type(self.parameters) is not QuPathSvmParameters:
            raise BatchTrainingError("parameters must be exact QuPathSvmParameters")
        if type(self.termination_flags) is not tuple or self.termination_flags != (
            "MAX_ITER",
            "EPS",
        ):
            raise BatchTrainingError("termination_flags must enable MAX_ITER and EPS")

    @classmethod
    def create(
        cls, *, parameters: QuPathSvmParameters, random_seed: int
    ) -> "QuPathNativeSvmExecutionProjection":
        return cls(
            schema="qupath-native-svm-execution/v1",
            native_engine="qupath_bundled_opencv",
            native_model_family="opencv_ml.SVM",
            training_path="raw_svm_train_then_qupath_public_wrap",
            seed_behavior="set_rng_seed_immediately_before_train",
            random_seed=random_seed,
            parameters=parameters,
            termination_flags=("MAX_ITER", "EPS"),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "QuPathNativeSvmExecutionProjection":
        mapping = _closed_mapping(value, fields=cls._FIELDS, field="qupath_native_svm_execution")
        return cls(
            schema=mapping["schema"],  # type: ignore[arg-type]
            native_engine=mapping["native_engine"],  # type: ignore[arg-type]
            native_model_family=mapping["native_model_family"],  # type: ignore[arg-type]
            training_path=mapping["training_path"],  # type: ignore[arg-type]
            seed_behavior=mapping["seed_behavior"],  # type: ignore[arg-type]
            random_seed=mapping["random_seed"],  # type: ignore[arg-type]
            parameters=QuPathSvmParameters.from_mapping(mapping["parameters"]),
            termination_flags=tuple(
                _array(mapping["termination_flags"], "termination_flags")
            ),  # type: ignore[arg-type]
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "native_engine": self.native_engine,
            "native_model_family": self.native_model_family,
            "training_path": self.training_path,
            "seed_behavior": self.seed_behavior,
            "random_seed": self.random_seed,
            "parameters": self.parameters.to_mapping(),
            "termination_flags": list(self.termination_flags),
        }


@dataclass(frozen=True, slots=True)
class QuPathPixelSvmConfiguration:
    """Closed pixel features and C-SVC controls for native QuPath training."""

    configuration_schema: Literal["qupath-pixel-svm/v1"]
    resolution_um_per_pixel: float
    image_features: PixelImageFeatures
    maximum_training_samples: int
    reweight_samples: bool
    normalization: Literal["none", "mean_variance", "min_max"]
    pca_retained_variance: float
    pca_normalize: bool
    boundary_strategy: Literal["skip", "classify"]
    boundary_thickness_px: float
    boundary_class_id: int | None
    output_labels: QuPathOutputLabels
    svm: QuPathSvmParameters

    def __post_init__(self) -> None:
        if self.configuration_schema != "qupath-pixel-svm/v1":
            raise BatchTrainingError("configuration_schema must be qupath-pixel-svm/v1")
        for name, value in _pixel_features(self).items():
            object.__setattr__(self, name, value)
        if type(self.svm) is not QuPathSvmParameters:
            raise BatchTrainingError("svm must be exact QuPathSvmParameters")

    @classmethod
    def from_mapping(cls, value: object) -> "QuPathPixelSvmConfiguration":
        mapping = _configuration_mapping(value, family="pixel", model_field="svm")
        feature_values = {
            name: mapping[name]
            for name in mapping
            if name not in {"configuration_schema", "svm", "output_labels", "boundary_class_id"}
        }
        return cls(
            configuration_schema=mapping["configuration_schema"],  # type: ignore[arg-type]
            **feature_values,  # type: ignore[arg-type]
            boundary_class_id=mapping.get("boundary_class_id"),  # type: ignore[arg-type]
            output_labels=QuPathOutputLabels.from_mapping(mapping["output_labels"]),
            svm=QuPathSvmParameters.from_mapping(mapping["svm"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "configuration_schema": self.configuration_schema,
            **_pixel_feature_mapping(self),
            "svm": self.svm.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class QuPathObjectSvmConfiguration:
    """Bind measured or frozen encoder features to native C-SVC controls."""

    configuration_schema: Literal["qupath-object-svm/v1"]
    object_filter: Literal["cells"]
    training_annotations: Literal["points"]
    output_classes: Literal["selected"]
    normalization: Literal["none", "mean_variance", "min_max"]
    pca_retained_variance: float
    multiclass: Literal[False]
    output_labels: QuPathOutputLabels
    svm: QuPathSvmParameters
    feature_source: object | None = None
    pca_normalize: bool = True

    def __post_init__(self) -> None:
        if self.configuration_schema != "qupath-object-svm/v1":
            raise BatchTrainingError("configuration_schema must be qupath-object-svm/v1")
        for name, value in _object_features(self).items():
            object.__setattr__(self, name, value)
        if type(self.svm) is not QuPathSvmParameters:
            raise BatchTrainingError("svm must be exact QuPathSvmParameters")
        if self.feature_source is not None:
            from .qupath_kronos_cell import KronosCellFeatureSource
            if type(self.feature_source) is not KronosCellFeatureSource:
                raise BatchTrainingError("feature_source must be exact KronosCellFeatureSource")

    @classmethod
    def from_mapping(cls, value: object) -> "QuPathObjectSvmConfiguration":
        if not isinstance(value, Mapping):
            raise BatchTrainingError("object SVM configuration must be an object")
        raw = dict(value)
        source = raw.pop("feature_source", None)
        mapping = _configuration_mapping(raw, family="object", model_field="svm")
        if source is not None:
            from .qupath_kronos_cell import KronosCellFeatureSource
            source = KronosCellFeatureSource.from_mapping(source)
        feature_values = {
            name: mapping[name]
            for name in mapping
            if name not in {"configuration_schema", "svm", "output_labels"}
        }
        return cls(
            configuration_schema=mapping["configuration_schema"],  # type: ignore[arg-type]
            **feature_values,  # type: ignore[arg-type]
            output_labels=QuPathOutputLabels.from_mapping(mapping["output_labels"]),
            svm=QuPathSvmParameters.from_mapping(mapping["svm"]),
            feature_source=source,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "configuration_schema": self.configuration_schema,
            **_object_feature_mapping(self),
            "svm": self.svm.to_mapping(),
            **({"feature_source": self.feature_source.to_mapping()} if self.feature_source is not None else {}),
        }
