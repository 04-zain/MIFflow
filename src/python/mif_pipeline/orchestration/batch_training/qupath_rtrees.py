"""Validate the native random-forest parameters stored with a QuPath training configuration.

The parameter record names tree depth, sample-count constraints, feature sampling, tree
count and termination settings. Feature preparation and output labels are validated in the
containing pixel/object configuration. New authoring values are loaded from the shared
preset file; serialized accepted values remain explicit."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from ._shared import BatchTrainingError, _array, _closed_mapping
from .native_projection import QuPathOutputLabels
from .qupath_feature_settings import (
    PixelImageFeatures,
    _boolean,
    _configuration_mapping,
    _finite_number,
    _integer,
    _object_feature_mapping,
    _object_features,
    _pixel_feature_mapping,
    _pixel_features,
)

# ==============================================================================
#                                 Qupath rtrees
# ==============================================================================


@dataclass(frozen=True, slots=True)
class QuPathRTreesParameters:
    """Every RTrees control exposed by the supported QuPath 0.7 runtime."""

    max_depth: int
    min_sample_count: int
    use_1se_rule: bool
    active_var_count: int
    max_trees: int
    termination_epsilon: float
    calculate_variable_importance: bool
    cv_folds: Literal[0]
    use_surrogates: Literal[False]

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "max_depth",
            "min_sample_count",
            "use_1se_rule",
            "active_var_count",
            "max_trees",
            "termination_epsilon",
            "calculate_variable_importance",
            "cv_folds",
            "use_surrogates",
        }
    )

    def __post_init__(self) -> None:
        """Validate the QuPathRTreesParameters fields when the record is constructed.

        The checks cover ``max_depth``, ``min_sample_count``, ``use_1se_rule``,
        ``active_var_count``, ``max_trees``, ``termination_epsilon``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller. Maximum depth must be positive so the trees can split."""

        object.__setattr__(self, "max_depth", _integer(self.max_depth, "max_depth", minimum=1))
        object.__setattr__(
            self,
            "min_sample_count",
            _integer(self.min_sample_count, "min_sample_count", minimum=1),
        )
        object.__setattr__(self, "use_1se_rule", _boolean(self.use_1se_rule, "use_1se_rule"))
        object.__setattr__(
            self,
            "active_var_count",
            _integer(self.active_var_count, "active_var_count", minimum=0),
        )
        object.__setattr__(self, "max_trees", _integer(self.max_trees, "max_trees", minimum=0))
        object.__setattr__(
            self,
            "termination_epsilon",
            _finite_number(
                self.termination_epsilon,
                "termination_epsilon",
                minimum=0.0,
                positive=False,
            ),
        )
        object.__setattr__(
            self,
            "calculate_variable_importance",
            _boolean(self.calculate_variable_importance, "calculate_variable_importance"),
        )
        if self.max_trees == 0 and self.termination_epsilon == 0.0:
            raise BatchTrainingError(
                "max_trees and termination_epsilon cannot both disable termination"
            )
        if type(self.cv_folds) is not int or self.cv_folds != 0:
            raise BatchTrainingError("cv_folds must be the fixed literal 0")
        if type(self.use_surrogates) is not bool or self.use_surrogates is not False:
            raise BatchTrainingError("use_surrogates must be the fixed literal false")

    @classmethod
    def from_mapping(cls, value: object) -> "QuPathRTreesParameters":
        mapping = _closed_mapping(value, fields=cls._FIELDS, field="qupath_rtrees")
        return cls(**{field: mapping[field] for field in cls._FIELDS})  # type: ignore[arg-type]

    @classmethod
    def authoring_defaults(cls) -> "QuPathRTreesParameters":
        """Read the packaged RF settings used to initialize the training form."""
        from mif_pipeline._defaults import default_section

        return cls.from_mapping(default_section("classifier.rtrees"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "max_depth": self.max_depth,
            "min_sample_count": self.min_sample_count,
            "use_1se_rule": self.use_1se_rule,
            "active_var_count": self.active_var_count,
            "max_trees": self.max_trees,
            "termination_epsilon": self.termination_epsilon,
            "calculate_variable_importance": self.calculate_variable_importance,
            "cv_folds": self.cv_folds,
            "use_surrogates": self.use_surrogates,
        }


@dataclass(frozen=True, slots=True)
class QuPathNativeRTreesExecutionProjection:
    """Exact raw-native training path required to preserve the request seed."""

    schema: Literal["qupath-native-rtrees-execution/v1"]
    native_engine: Literal["qupath_bundled_opencv"]
    native_model_family: Literal["opencv_ml.RTrees"]
    training_path: Literal["raw_rtrees_train_then_qupath_public_wrap"]
    seed_behavior: Literal["set_rng_seed_immediately_before_train"]
    random_seed: int
    termination_flags: tuple[Literal["MAX_ITER", "EPS"], ...]
    termination_max_count: int
    termination_epsilon: float
    cv_folds: Literal[0]
    use_surrogates: Literal[False]

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema",
            "native_engine",
            "native_model_family",
            "training_path",
            "seed_behavior",
            "random_seed",
            "termination_flags",
            "termination_max_count",
            "termination_epsilon",
            "cv_folds",
            "use_surrogates",
        }
    )

    def __post_init__(self) -> None:
        """Validate the QuPathNativeRTreesExecutionProjection fields when the record is
        constructed.

        The checks cover ``termination_max_count``, ``termination_epsilon``,
        ``random_seed``, ``termination_flags``, ``cv_folds``, ``use_surrogates``. Invalid
        field types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        fixed = {
            "schema": "qupath-native-rtrees-execution/v1",
            "native_engine": "qupath_bundled_opencv",
            "native_model_family": "opencv_ml.RTrees",
            "training_path": "raw_rtrees_train_then_qupath_public_wrap",
            "seed_behavior": "set_rng_seed_immediately_before_train",
        }
        for field, expected in fixed.items():
            if getattr(self, field) != expected:
                raise BatchTrainingError(f"{field} must be the fixed literal {expected}")
        object.__setattr__(
            self, "random_seed", _integer(self.random_seed, "random_seed", minimum=0)
        )
        max_count = _integer(
            self.termination_max_count,
            "termination_max_count",
            minimum=0,
        )
        epsilon = _finite_number(
            self.termination_epsilon,
            "termination_epsilon",
            minimum=0.0,
            positive=False,
        )
        expected_flags = (
            *(("MAX_ITER",) if max_count > 0 else ()),
            *(("EPS",) if epsilon > 0.0 else ()),
        )
        if not expected_flags:
            raise BatchTrainingError("native termination criteria cannot be disabled")
        if tuple(self.termination_flags) != expected_flags:
            raise BatchTrainingError(
                "termination_flags must be computed directly from max count and epsilon"
            )
        if type(self.cv_folds) is not int or self.cv_folds != 0:
            raise BatchTrainingError("cv_folds must be the fixed literal 0")
        if type(self.use_surrogates) is not bool or self.use_surrogates is not False:
            raise BatchTrainingError("use_surrogates must be the fixed literal false")
        object.__setattr__(self, "termination_flags", expected_flags)
        object.__setattr__(self, "termination_max_count", max_count)
        object.__setattr__(self, "termination_epsilon", epsilon)

    @classmethod
    def create(
        cls,
        *,
        parameters: QuPathRTreesParameters,
        random_seed: int,
    ) -> "QuPathNativeRTreesExecutionProjection":
        if type(parameters) is not QuPathRTreesParameters:
            raise BatchTrainingError("parameters must be exact QuPathRTreesParameters")
        flags = (
            *(("MAX_ITER",) if parameters.max_trees > 0 else ()),
            *(("EPS",) if parameters.termination_epsilon > 0.0 else ()),
        )
        return cls(
            schema="qupath-native-rtrees-execution/v1",
            native_engine="qupath_bundled_opencv",
            native_model_family="opencv_ml.RTrees",
            training_path="raw_rtrees_train_then_qupath_public_wrap",
            seed_behavior="set_rng_seed_immediately_before_train",
            random_seed=random_seed,
            termination_flags=flags,  # type: ignore[arg-type]
            termination_max_count=parameters.max_trees,
            termination_epsilon=parameters.termination_epsilon,
            cv_folds=parameters.cv_folds,
            use_surrogates=parameters.use_surrogates,
        )

    @classmethod
    def from_mapping(cls, value: object) -> "QuPathNativeRTreesExecutionProjection":
        mapping = _closed_mapping(value, fields=cls._FIELDS, field="qupath_native_rtrees_execution")
        return cls(
            schema=mapping["schema"],  # type: ignore[arg-type]
            native_engine=mapping["native_engine"],  # type: ignore[arg-type]
            native_model_family=mapping["native_model_family"],  # type: ignore[arg-type]
            training_path=mapping["training_path"],  # type: ignore[arg-type]
            seed_behavior=mapping["seed_behavior"],  # type: ignore[arg-type]
            random_seed=mapping["random_seed"],  # type: ignore[arg-type]
            termination_flags=tuple(
                _array(mapping["termination_flags"], "termination_flags")
            ),  # type: ignore[arg-type]
            termination_max_count=mapping["termination_max_count"],  # type: ignore[arg-type]
            termination_epsilon=mapping["termination_epsilon"],  # type: ignore[arg-type]
            cv_folds=mapping["cv_folds"],  # type: ignore[arg-type]
            use_surrogates=mapping["use_surrogates"],  # type: ignore[arg-type]
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "native_engine": self.native_engine,
            "native_model_family": self.native_model_family,
            "training_path": self.training_path,
            "seed_behavior": self.seed_behavior,
            "random_seed": self.random_seed,
            "termination_flags": list(self.termination_flags),
            "termination_max_count": self.termination_max_count,
            "termination_epsilon": self.termination_epsilon,
            "cv_folds": self.cv_folds,
            "use_surrogates": self.use_surrogates,
        }


@dataclass(frozen=True, slots=True)
class QuPathPixelRTreesConfiguration:
    """Closed fit-affecting configuration for QuPath pixel RTrees."""

    configuration_schema: Literal["qupath-pixel-rtrees/v1"]
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
    rtrees: QuPathRTreesParameters

    def __post_init__(self) -> None:
        if self.configuration_schema != "qupath-pixel-rtrees/v1":
            raise BatchTrainingError("configuration_schema must be qupath-pixel-rtrees/v1")
        for name, value in _pixel_features(self).items():
            object.__setattr__(self, name, value)
        if not isinstance(self.rtrees, QuPathRTreesParameters):
            raise BatchTrainingError("rtrees must be QuPathRTreesParameters")

    @classmethod
    def from_mapping(cls, value: object) -> "QuPathPixelRTreesConfiguration":
        mapping = _configuration_mapping(value, family="pixel", model_field="rtrees")
        return cls(
            configuration_schema=mapping["configuration_schema"],  # type: ignore[arg-type]
            image_features=PixelImageFeatures.from_mapping(mapping["image_features"]),
            resolution_um_per_pixel=mapping["resolution_um_per_pixel"],  # type: ignore[arg-type]
            maximum_training_samples=mapping["maximum_training_samples"],  # type: ignore[arg-type]
            reweight_samples=mapping["reweight_samples"],  # type: ignore[arg-type]
            normalization=mapping["normalization"],  # type: ignore[arg-type]
            pca_retained_variance=mapping["pca_retained_variance"],  # type: ignore[arg-type]
            pca_normalize=mapping["pca_normalize"],  # type: ignore[arg-type]
            boundary_strategy=mapping["boundary_strategy"],  # type: ignore[arg-type]
            boundary_thickness_px=mapping["boundary_thickness_px"],  # type: ignore[arg-type]
            boundary_class_id=mapping.get("boundary_class_id"),  # type: ignore[arg-type]
            output_labels=QuPathOutputLabels.from_mapping(mapping["output_labels"]),
            rtrees=QuPathRTreesParameters.from_mapping(mapping["rtrees"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "configuration_schema": self.configuration_schema,
            **_pixel_feature_mapping(self),
            "rtrees": self.rtrees.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class QuPathObjectRTreesConfiguration:
    """Closed fit-affecting configuration for QuPath object RTrees."""

    configuration_schema: Literal["qupath-object-rtrees/v1"]
    object_filter: Literal["cells"]
    training_annotations: Literal["points"]
    output_classes: Literal["selected"]
    normalization: Literal["none", "mean_variance", "min_max"]
    pca_retained_variance: float
    multiclass: Literal[False]
    output_labels: QuPathOutputLabels
    rtrees: QuPathRTreesParameters
    feature_source: object | None = None
    pca_normalize: bool = True

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "configuration_schema",
            "object_filter",
            "training_annotations",
            "output_classes",
            "normalization",
            "pca_retained_variance",
            "multiclass",
            "output_labels",
            "rtrees",
        }
    )

    def __post_init__(self) -> None:
        if self.configuration_schema != "qupath-object-rtrees/v1":
            raise BatchTrainingError("configuration_schema must be qupath-object-rtrees/v1")
        for name, value in _object_features(self).items():
            object.__setattr__(self, name, value)
        if not isinstance(self.rtrees, QuPathRTreesParameters):
            raise BatchTrainingError("rtrees must be QuPathRTreesParameters")
        if self.feature_source is not None:
            from .qupath_kronos_cell import KronosCellFeatureSource
            if type(self.feature_source) is not KronosCellFeatureSource:
                raise BatchTrainingError("feature_source must be exact KronosCellFeatureSource")

    @classmethod
    def from_mapping(cls, value: object) -> "QuPathObjectRTreesConfiguration":
        from collections.abc import Mapping
        if not isinstance(value, Mapping):
            raise BatchTrainingError("object RF configuration must be an object")
        raw = dict(value)
        source = raw.pop("feature_source", None)
        mapping = _configuration_mapping(raw, family="object", model_field="rtrees")
        if source is not None:
            from .qupath_kronos_cell import KronosCellFeatureSource
            source = KronosCellFeatureSource.from_mapping(source)
        return cls(
            configuration_schema=mapping["configuration_schema"],  # type: ignore[arg-type]
            object_filter=mapping["object_filter"],  # type: ignore[arg-type]
            training_annotations=mapping["training_annotations"],  # type: ignore[arg-type]
            output_classes=mapping["output_classes"],  # type: ignore[arg-type]
            normalization=mapping["normalization"],  # type: ignore[arg-type]
            pca_retained_variance=mapping["pca_retained_variance"],  # type: ignore[arg-type]
            multiclass=mapping["multiclass"],  # type: ignore[arg-type]
            output_labels=QuPathOutputLabels.from_mapping(mapping["output_labels"]),
            rtrees=QuPathRTreesParameters.from_mapping(mapping["rtrees"]),
            feature_source=source,
            pca_normalize=mapping.get("pca_normalize", True),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "configuration_schema": self.configuration_schema,
            **_object_feature_mapping(self),
            "rtrees": self.rtrees.to_mapping(),
            **({"feature_source": self.feature_source.to_mapping()} if self.feature_source is not None else {}),
        }
