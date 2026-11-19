"""Validate the finite candidate inventory and subject-fold configuration of a model search.

A candidate contains the complete conventional or KRONOS/native model settings. The search
records its scoring rule and fold count, rejects duplicate or excessive candidates and
preserves their deterministic ordering. Execution refits each candidate's learned
transformations inside the corresponding training fold."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from dataclasses import dataclass
from collections.abc import Mapping
import json, hashlib
from ._shared import BatchTrainingError, _closed_mapping
from .qupath_rtrees import QuPathPixelRTreesConfiguration, QuPathObjectRTreesConfiguration
from .qupath_svm import QuPathPixelSvmConfiguration, QuPathObjectSvmConfiguration
from .qupath_logistic_regression import QuPathObjectLogisticRegressionConfiguration
from .qupath_kronos import QuPathKronosConfiguration, native_estimator_configuration as _native
from mif_pipeline.pipeline_core.model_selection import CV_SCHEMA, CV_SCORE

_PARSERS = {
    "qupath-object-rtrees/v1": QuPathObjectRTreesConfiguration,
    "qupath-object-svm/v1": QuPathObjectSvmConfiguration,
    "qupath-object-logistic-regression/v1": QuPathObjectLogisticRegressionConfiguration,
    "qupath-pixel-rtrees/v1": QuPathPixelRTreesConfiguration,
    "qupath-pixel-svm/v1": QuPathPixelSvmConfiguration,
    "qupath-kronos-rtrees/v2": QuPathKronosConfiguration,
    "qupath-kronos-svm/v2": QuPathKronosConfiguration,
}


# ==============================================================================
#                                Cross validation
# ==============================================================================


def _identity(candidate):
    row = candidate.to_mapping()
    if type(candidate) is QuPathKronosConfiguration:
        row.pop("graph")
        native = row["native_configuration"]
    else:
        native = row
    native.pop("rtrees", None)
    native.pop("svm", None)
    native.pop("logistic_regression", None)
    return row


@dataclass(frozen=True, slots=True)
class QuPathGroupedCVConfiguration:
    configuration_schema: str
    folds: int
    scoring: str
    candidates: tuple

    def __post_init__(self):
        if self.configuration_schema != CV_SCHEMA or self.scoring != CV_SCORE:
            raise BatchTrainingError("Select the supported subject-grouped model selection rule")
        if type(self.folds) is not int or self.folds not in (0, *range(2, 11)):
            raise BatchTrainingError("Cross-validation uses automatic or 2–10 subject folds")
        if (
            type(self.candidates) is not tuple
            or not 1 <= len(self.candidates) <= 12
            or any(type(c) not in set(_PARSERS.values()) for c in self.candidates)
        ):
            raise BatchTrainingError("Select 1–12 explicit supported classifier candidates")
        identity = _identity(self.candidates[0])
        if any(_identity(c) != identity for c in self.candidates):
            raise BatchTrainingError(
                "Search candidates must share their data definition, class labels, image resolution and encoder convention"
            )
        keys = [
            json.dumps(c.to_mapping(), sort_keys=True, allow_nan=False) for c in self.candidates
        ]
        if len(set(keys)) != len(keys):
            raise BatchTrainingError("Cross-validation candidates cannot be duplicates")

    @property
    def base_configuration(self):
        return self.candidates[0]

    @property
    def output_labels(self):
        return self.base_configuration.output_labels

    @property
    def engine_id(self):
        base = self.base_configuration
        if type(base) is QuPathKronosConfiguration:
            return base.engine_id
        object_engines = {QuPathObjectRTreesConfiguration: "qupath-object-classifier",
            QuPathObjectSvmConfiguration: "qupath-object-svm-classifier",
            QuPathObjectLogisticRegressionConfiguration: "qupath-object-logistic-regression-classifier"}
        if type(base) in object_engines:
            return object_engines[type(base)]
        return (
            "qupath-pixel-svm-classifier"
            if type(base) is QuPathPixelSvmConfiguration
            else "qupath-pixel-classifier"
        )

    @classmethod
    def from_mapping(cls, value):
        row = _closed_mapping(
            value,
            fields=frozenset(cls.__dataclass_fields__),
            field="grouped cross-validation configuration",
        )
        if not isinstance(row["candidates"], (list, tuple)):
            raise BatchTrainingError("Cross-validation candidates must be an array")
        candidates = []
        for candidate in row["candidates"]:
            parser = (
                _PARSERS.get(candidate.get("configuration_schema"))
                if isinstance(candidate, Mapping)
                else None
            )
            if parser is None:
                raise BatchTrainingError(
                    "Nested searches or unsupported model families are not allowed"
                )
            candidates.append(parser.from_mapping(candidate))
        return cls(row["configuration_schema"], row["folds"], row["scoring"], tuple(candidates))

    def to_mapping(self):
        return {
            "configuration_schema": self.configuration_schema,
            "folds": self.folds,
            "scoring": self.scoring,
            "candidates": [c.to_mapping() for c in self.candidates],
        }


def base_configuration(configuration):
    return (
        configuration.base_configuration
        if type(configuration) is QuPathGroupedCVConfiguration
        else configuration
    )


def native_estimator_configuration(configuration):
    return _native(base_configuration(configuration))


@dataclass(frozen=True, slots=True)
class QuPathGroupedCVExecutionProjection:
    schema: str
    random_seed: int
    search_configuration_sha256: str
    refit_policy: str = "fresh_fit_all_development_subjects"

    def __post_init__(self):
        if (
            self.schema != "qupath-grouped-cv-execution/v1"
            or type(self.random_seed) is not int
            or not 0 <= self.random_seed <= 2147483647
        ):
            raise BatchTrainingError("Invalid grouped cross-validation execution projection")
        if len(self.search_configuration_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.search_configuration_sha256
        ):
            raise BatchTrainingError("Search configuration requires an exact SHA-256")
        if self.refit_policy != "fresh_fit_all_development_subjects":
            raise BatchTrainingError(
                "The selected model must be refitted on all development subjects"
            )

    @classmethod
    def create(cls, configuration, random_seed):
        from ._shared import _digest

        if type(configuration) is not QuPathGroupedCVConfiguration:
            raise BatchTrainingError("An exact search configuration is required")
        return cls(
            "qupath-grouped-cv-execution/v1", random_seed, _digest(configuration.to_mapping())
        )

    @classmethod
    def from_mapping(cls, value):
        row = _closed_mapping(
            value, fields=frozenset(cls.__dataclass_fields__), field="grouped CV execution"
        )
        return cls(**row)

    def to_mapping(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def compact_selection_summary(report, subjects, classes, candidate_count, folds, seed):
    """Recheck the native owner's retained report before exposing a compact review."""
    from mif_pipeline.pipeline_core.model_selection import subject_folds, build_selection_report
    from ._shared import _digest
    import math

    try:
        assignment = subject_folds(subjects, folds, seed)
        rows = report["candidates"]
        if len(rows) != candidate_count:
            raise ValueError("candidate count")
        expected = build_selection_report(
            assignment, classes, [r["subject_confusions"] for r in rows], seed
        )

        def equivalent(a, b):
            if isinstance(b, dict):
                return (
                    isinstance(a, Mapping)
                    and set(a) == set(b)
                    and all(equivalent(a[k], v) for k, v in b.items())
                )
            if isinstance(b, list):
                return (
                    isinstance(a, (list, tuple))
                    and len(a) == len(b)
                    and all(equivalent(x, y) for x, y in zip(a, b))
                )
            if type(b) is float:
                return type(a) in (float, int) and math.isfinite(a) and abs(a - b) < 1e-10
            return type(a) is type(b) and a == b

        if "reference_observations" in report:
            from mif_pipeline.pipeline_core.model_selection import validate_cv_reference_observations
            expected["reference_observations"] = validate_cv_reference_observations(
                report["reference_observations"], expected)
        if "fold_resources" in report:
            from mif_pipeline.pipeline_core.model_selection import validate_cv_fold_resources
            expected["fold_resources"] = validate_cv_fold_resources(report["fold_resources"], expected)
        if "pixel_scores" in report:
            from mif_pipeline.compartment_analysis.precision_recall import validate_cv_pixel_scores
            expected["pixel_scores"] = validate_cv_pixel_scores(report["pixel_scores"], expected)
        if not equivalent(report, expected):
            raise ValueError("report fields or scores")
    except (ValueError, KeyError, TypeError) as exc:
        raise BatchTrainingError(
            f"The current grouped-CV report does not match its development cohort: {exc}"
        ) from exc
    return {
        "schema": "mif-cv-review-summary/v1",
        "fold_count": len(set(assignment.values())),
        "development_subject_count": len(assignment),
        "selected_candidate_index": expected["selected_candidate_index"],
        "selection_score": expected["selection_score"],
        "candidate_scores": [
            {k: r[k] for k in ("candidate_index", "selection_score", "fold_scores")}
            for r in expected["candidates"]
        ],
        "report_sha256": _digest(report),
        "interpretation": "tuning_not_independent_test",
        "refit_scope": "all_development_subjects",
        "scoring": expected["scoring"],
    }


def validate_compact_selection_summary(value, development_subjects):
    from ._shared import _sha256
    import math

    fields = {
        "schema",
        "fold_count",
        "development_subject_count",
        "selected_candidate_index",
        "selection_score",
        "candidate_scores",
        "report_sha256",
        "interpretation",
        "refit_scope",
        "scoring",
    }
    row = _closed_mapping(value, fields=frozenset(fields), field="cross-validation review summary")
    if (
        row["schema"] != "mif-cv-review-summary/v1"
        or row["interpretation"] != "tuning_not_independent_test"
        or row["refit_scope"] != "all_development_subjects"
        or row["scoring"] != CV_SCORE
        or type(row["development_subject_count"]) is not int
        or row["development_subject_count"] != development_subjects
        or type(row["fold_count"]) is not int
        or not 2 <= row["fold_count"] <= min(10, development_subjects)
    ):
        raise BatchTrainingError("The CV review scope or interpretation is invalid")
    _sha256(row["report_sha256"], "CV report digest")
    rows = row["candidate_scores"]
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 12:
        raise BatchTrainingError("The candidate score inventory is invalid")
    result = []
    for index, raw in enumerate(rows):
        r = _closed_mapping(
            raw,
            fields=frozenset({"candidate_index", "fold_scores", "selection_score"}),
            field="candidate score",
        )
        if (
            type(r["candidate_index"]) is not int
            or r["candidate_index"] != index
            or len(r["fold_scores"]) != row["fold_count"]
        ):
            raise BatchTrainingError("Candidate order or fold count changed")
        if any(
            type(s) not in (float, int) or not math.isfinite(s) or not 0 <= s <= 1
            for s in (*r["fold_scores"], r["selection_score"])
        ):
            raise BatchTrainingError("The candidate score is outside its supported range")
        result.append(
            {
                "candidate_index": index,
                "selection_score": r["selection_score"],
                "fold_scores": list(r["fold_scores"]),
            }
        )
    best = max(range(len(result)), key=lambda i: (result[i]["selection_score"], -i))
    if (
        row["selected_candidate_index"] != best
        or row["selection_score"] != result[best]["selection_score"]
    ):
        raise BatchTrainingError(
            "The displayed selected candidate differs from the recorded selection rule"
        )
    return {**dict(row), "candidate_scores": result}
