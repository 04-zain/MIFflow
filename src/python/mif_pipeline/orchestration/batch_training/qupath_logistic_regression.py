"""Specify the OpenCV logistic cell classifier and its reproducible native fit.

We fit one logistic response for each class in the QuPath JVM using the selected
cell feature table. Normalization and PCA belong to the fitted feature extractor.
The classifier retains its learned coefficients and returns the largest normalized
one-versus-rest sigmoid score. Those scores describe this model's class support;
their agreement with observed frequencies requires a labelled calibration study.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from typing import ClassVar

from ._shared import BatchTrainingError, _closed_mapping
from .native_projection import QuPathOutputLabels
from .qupath_feature_settings import _configuration_mapping, _object_features, _object_feature_mapping
from .qupath_svm import _positive_finite, _java_integer


@dataclass(frozen=True, slots=True)
class QuPathLogisticRegressionParameters:
    """Retain full-batch controls; epsilon is recorded but does not stop BATCH fits."""

    learning_rate: float
    iterations: int
    regularization: str
    training_method: str
    termination_epsilon: float
    _FIELDS: ClassVar[frozenset[str]] = frozenset({
        'learning_rate', 'iterations', 'regularization', 'training_method', 'termination_epsilon'})

    def __post_init__(self):
        """Require finite positive step/iteration settings and supported native enums."""
        object.__setattr__(self, 'learning_rate', _positive_finite(self.learning_rate, 'learning_rate'))
        object.__setattr__(self, 'iterations', _java_integer(self.iterations, 'iterations', minimum=1))
        object.__setattr__(self, 'termination_epsilon', _positive_finite(self.termination_epsilon, 'termination_epsilon'))
        if self.regularization not in ('L2', 'DISABLE'):
            raise BatchTrainingError('regularization must be L2 or DISABLE')
        if self.training_method != 'BATCH':
            raise BatchTrainingError('training_method must be BATCH')

    @classmethod
    def from_mapping(cls, value):
        """Decode every optimizer setting, rejecting extra or missing parameters."""
        return cls(**_closed_mapping(value, fields=cls._FIELDS, field='qupath_logistic_regression'))

    @classmethod
    def authoring_defaults(cls):
        """Read editable defaults; accepted fit requests always contain explicit values."""
        from mif_pipeline._defaults import default_section
        return cls.from_mapping(default_section('classifier.logistic_regression'))

    def to_mapping(self):
        """Return the checked optimizer controls for the native request and model record."""
        return {field: getattr(self, field) for field in self._FIELDS}


@dataclass(frozen=True, slots=True)
class QuPathObjectLogisticRegressionConfiguration:
    """Bind selected cell classes, preprocessing and optional frozen image features."""

    configuration_schema: str
    object_filter: str
    training_annotations: str
    output_classes: str
    normalization: str
    pca_retained_variance: float
    multiclass: bool
    output_labels: QuPathOutputLabels
    logistic_regression: QuPathLogisticRegressionParameters
    feature_source: object | None = None
    pca_normalize: bool = True

    def __post_init__(self):
        """Validate object preprocessing and retain one unambiguous feature source."""
        if self.configuration_schema != 'qupath-object-logistic-regression/v1':
            raise BatchTrainingError('configuration_schema must be qupath-object-logistic-regression/v1')
        for name, value in _object_features(self).items():
            object.__setattr__(self, name, value)
        if type(self.logistic_regression) is not QuPathLogisticRegressionParameters:
            raise BatchTrainingError('logistic_regression must be exact QuPathLogisticRegressionParameters')
        if self.feature_source is not None:
            from .qupath_kronos_cell import KronosCellFeatureSource
            if type(self.feature_source) is not KronosCellFeatureSource:
                raise BatchTrainingError('feature_source must be exact KronosCellFeatureSource')

    @classmethod
    def from_mapping(cls, value):
        """Decode classifier settings and validate the optional KRONOS extraction contract."""
        if not isinstance(value, Mapping):
            raise BatchTrainingError('object logistic configuration must be an object')
        raw = dict(value)
        source = raw.pop('feature_source', None)
        row = _configuration_mapping(raw, family='object', model_field='logistic_regression')
        if source is not None:
            from .qupath_kronos_cell import KronosCellFeatureSource
            source = KronosCellFeatureSource.from_mapping(source)
        return cls(**{k:v for k,v in row.items() if k not in {'output_labels','logistic_regression'}},
            output_labels=QuPathOutputLabels.from_mapping(row['output_labels']),
            logistic_regression=QuPathLogisticRegressionParameters.from_mapping(row['logistic_regression']),
            feature_source=source)

    def to_mapping(self):
        """Save class order, preprocessing and encoder identity with the native parameters."""
        row = {'configuration_schema':self.configuration_schema,
            **_object_feature_mapping(self), 'logistic_regression':self.logistic_regression.to_mapping()}
        if self.feature_source is not None:
            row['feature_source'] = self.feature_source.to_mapping()
        return row


@dataclass(frozen=True, slots=True)
class QuPathNativeLogisticRegressionExecutionProjection:
    """Record native fitting controls; BATCH uses iterations and ignores the EPS flag."""

    schema: str
    native_engine: str
    native_model_family: str
    training_path: str
    seed_behavior: str
    random_seed: int
    parameters: QuPathLogisticRegressionParameters
    termination_flags: tuple[str, str]

    def __post_init__(self):
        """Require the declared OpenCV full-batch route and a complete optimizer record."""
        fixed = {'schema':'qupath-native-logistic-regression-execution/v1',
            'native_engine':'qupath_bundled_opencv', 'native_model_family':'opencv_ml.LogisticRegression',
            'training_path':'raw_logistic_regression_train_then_native_object_wrap',
            'seed_behavior':'set_rng_seed_immediately_before_train'}
        for field, expected in fixed.items():
            if getattr(self, field) != expected:
                raise BatchTrainingError(f'{field} must be {expected}')
        _java_integer(self.random_seed, 'random_seed', minimum=0)
        if type(self.parameters) is not QuPathLogisticRegressionParameters:
            raise BatchTrainingError('parameters must be exact QuPathLogisticRegressionParameters')
        if self.termination_flags != ('MAX_ITER', 'EPS'):
            raise BatchTrainingError('termination_flags must enable MAX_ITER and EPS')

    @classmethod
    def create(cls, *, parameters, random_seed):
        """Bind the accepted parameters and seed to the native implementation identity."""
        return cls('qupath-native-logistic-regression-execution/v1', 'qupath_bundled_opencv',
            'opencv_ml.LogisticRegression', 'raw_logistic_regression_train_then_native_object_wrap',
            'set_rng_seed_immediately_before_train', random_seed, parameters, ('MAX_ITER','EPS'))

    @classmethod
    def from_mapping(cls, value):
        """Decode the complete native execution record and recheck its fixed route."""
        row = dict(_closed_mapping(value, fields=frozenset(cls.__dataclass_fields__), field='native logistic execution'))
        row['parameters'] = QuPathLogisticRegressionParameters.from_mapping(row['parameters'])
        row['termination_flags'] = tuple(row['termination_flags'])
        return cls(**row)

    def to_mapping(self):
        """Serialize native parameters and stopping flags without process-local objects."""
        return {name:(self.parameters.to_mapping() if name=='parameters' else
            list(self.termination_flags) if name=='termination_flags' else getattr(self,name))
            for name in self.__dataclass_fields__}
