"""Closed response grammar for training-plan selection.

The `TrainingPlanCandidateSelection` record carries `candidate_id`, `roles`,
`evaluation_intent`, `model_configuration`, `random_seed` and its remaining declared fields.

Training records keep subject roles, reference evidence, model settings and application
scope linked. Their validators and native adapters are called by the shared training service
before a fit, evaluation or accepted publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
from sys import float_info
from types import MappingProxyType

from ...pipeline_core.config import PrimaryClassifierSettings
from ._shared import BatchTrainingError, EvaluationIntent, _export_json

TRAINING_PLAN_SELECTION_SCHEMA_VERSION = "training-plan-selection/v4"
TRAINING_PLAN_SELECTION_REQUEST_SCHEMA_VERSION = "training-plan-selection-request/v8"


# ==============================================================================
#                                 Plan selection
# ==============================================================================


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchTrainingError(f"{field} must be nonempty canonical text")
    return value


def validate_reference_class_names(names: Sequence[str]) -> None:
    """Require new annotation labels to round-trip through QuPath's class grammar."""
    for name in names:
        parts = name.split(":")
        if ("\n" in name or "\r" in name or any(not part.strip() for part in parts)
                or name != ": ".join(part.strip() for part in parts)):
            raise BatchTrainingError(
                f"Reference class {name!r} would change when saved in QuPath. "
                "Use a single-line name; separate hierarchy levels with ': ' "
                "and give every level a nonempty name."
            )


def _freeze_json(value: object, field: str) -> object:
    def export(item: object) -> object:
        if isinstance(item, Mapping):
            if any(type(key) is not str for key in item):
                raise BatchTrainingError(f"{field} must have string keys")
            return {key: export(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [export(child) for child in item]
        return item

    try:
        copied = json.loads(
            json.dumps(
                export(value),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise BatchTrainingError(f"{field} must be finite JSON") from exc

    def freeze(item: object) -> object:
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                raise BatchTrainingError(f"{field} must have string keys")
            return MappingProxyType({key: freeze(child) for key, child in sorted(item.items())})
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(copied)


def _closed(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise BatchTrainingError(f"{field} must be a string-keyed object")
    if set(value) != fields:
        raise BatchTrainingError(f"{field} has a foreign field inventory")
    return value


@dataclass(frozen=True, slots=True)
class TrainingPlanRoleSelection:
    project_entry_id: str
    split: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_entry_id",
            _text(self.project_entry_id, "project_entry_id"),
        )
        if self.split not in {"training", "validation", "test"}:
            raise BatchTrainingError("training plan role must be training, validation, or test")

    @classmethod
    def from_mapping(cls, value: object) -> "TrainingPlanRoleSelection":
        row = _closed(
            value,
            frozenset({"project_entry_id", "split"}),
            "training plan role",
        )
        return cls(str(row["project_entry_id"]), str(row["split"]))

    def to_mapping(self) -> dict[str, str]:
        return {
            "project_entry_id": self.project_entry_id,
            "split": self.split,
        }


@dataclass(frozen=True, slots=True)
class TrainingPlanCandidateSelection:
    candidate_id: str
    roles: tuple[TrainingPlanRoleSelection, ...]
    evaluation_intent: EvaluationIntent
    model_configuration: Mapping[str, object]
    random_seed: int
    primary_settings: PrimaryClassifierSettings | None
    supervised_class_ids: tuple[str, ...] | None
    supervised_feature_ids: tuple[str, ...] | None
    secondary_channel_roles: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        """Validate the TrainingPlanCandidateSelection fields when the record is constructed.

        The checks cover ``roles``, ``evaluation_intent``, ``model_configuration``,
        ``candidate_id``, ``random_seed``, ``primary_settings``. Invalid field types, values
        or combinations raise the declared validation error before this instance can be used
        by its caller."""

        object.__setattr__(self, "candidate_id", _text(self.candidate_id, "candidate_id"))
        if not isinstance(self.evaluation_intent, EvaluationIntent):
            raise BatchTrainingError("evaluation_intent is invalid")
        roles = tuple(self.roles)
        if (
            not roles
            or not all(type(item) is TrainingPlanRoleSelection for item in roles)
            or roles != tuple(sorted(roles, key=lambda item: (item.project_entry_id, item.split)))
            or len({(item.project_entry_id, item.split) for item in roles}) != len(roles)
        ):
            raise BatchTrainingError("training plan roles must be canonical and unique")
        splits = {item.split for item in roles}
        if self.evaluation_intent is EvaluationIntent.TRAINING_ONLY:
            if splits != {"training"}:
                raise BatchTrainingError(
                    "training-only roles require training and forbid validation or test"
                )
        elif self.evaluation_intent is EvaluationIntent.INDEPENDENT_PERFORMANCE:
            if not {"training", "test"} <= splits:
                raise BatchTrainingError(
                    "independent training plan roles require training and test"
                )
        elif not {"training", "validation"} <= splits:
            raise BatchTrainingError("training plan roles require training and validation")
        frozen = _freeze_json(self.model_configuration, "model_configuration")
        if not isinstance(frozen, Mapping) or not frozen:
            raise BatchTrainingError("model_configuration must be a nonempty JSON object")
        if type(self.random_seed) is not int:
            raise BatchTrainingError("random_seed must be an integer")
        if (
            self.primary_settings is not None
            and type(self.primary_settings) is not PrimaryClassifierSettings
        ):
            raise BatchTrainingError("primary_settings must be concrete primary settings or null")
        if (self.supervised_class_ids is None) != (self.supervised_feature_ids is None):
            raise BatchTrainingError("supervised classes and features must be selected together")
        for field in ("supervised_class_ids", "supervised_feature_ids"):
            values = getattr(self, field)
            if values is not None:
                if type(values) is not tuple or not values or len(values) > (768 if field == "supervised_feature_ids" else 512):
                    raise BatchTrainingError(f"{field} must be a nonempty bounded array")
                checked = tuple(_text(item, field) for item in values)
                if len(set(checked)) != len(checked):
                    raise BatchTrainingError(f"{field} must be unique")
        if self.secondary_channel_roles is not None:
            channels = _secondary_channel_roles(self.secondary_channel_roles, allow_empty=False)
            if self.primary_settings is not None or self.supervised_class_ids is not None:
                raise BatchTrainingError("Secondary input channels are foreign to this classifier")
            object.__setattr__(self, "secondary_channel_roles", MappingProxyType(channels))
        object.__setattr__(self, "roles", roles)
        object.__setattr__(self, "model_configuration", frozen)

    @classmethod
    def from_mapping(cls, value: object) -> "TrainingPlanCandidateSelection":
        """Decode the serialized fields into a validated TrainingPlanCandidateSelection record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        row = _closed(
            value,
            frozenset(
                {
                    "candidate_id",
                    "roles",
                    "evaluation_intent",
                    "model_configuration",
                    "random_seed",
                    "primary_settings",
                    "supervised_class_ids",
                    "supervised_feature_ids",
                    "secondary_channel_roles",
                }
            ),
            "training plan candidate selection",
        )
        raw_roles = row["roles"]
        if isinstance(raw_roles, (str, bytes)) or not isinstance(raw_roles, Sequence):
            raise BatchTrainingError("roles must be an array")
        try:
            intent = EvaluationIntent(str(row["evaluation_intent"]))
        except ValueError as exc:
            raise BatchTrainingError("evaluation_intent is invalid") from exc
        try:
            settings = (
                None
                if row["primary_settings"] is None
                else PrimaryClassifierSettings.from_mapping(row["primary_settings"])
            )
        except ValueError as exc:
            raise BatchTrainingError("primary_settings are invalid") from exc
        return cls(
            candidate_id=str(row["candidate_id"]),
            roles=tuple(TrainingPlanRoleSelection.from_mapping(item) for item in raw_roles),
            evaluation_intent=intent,
            model_configuration=row["model_configuration"],  # type: ignore[arg-type]
            random_seed=row["random_seed"],  # type: ignore[arg-type]
            primary_settings=settings,
            supervised_class_ids=_selection_ids(
                row["supervised_class_ids"], "supervised_class_ids"
            ),
            supervised_feature_ids=_selection_ids(
                row["supervised_feature_ids"], "supervised_feature_ids"
            ),
            secondary_channel_roles=row["secondary_channel_roles"],
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "roles": [item.to_mapping() for item in self.roles],
            "evaluation_intent": self.evaluation_intent.value,
            "model_configuration": _export_json(self.model_configuration),
            "random_seed": self.random_seed,
            "primary_settings": (
                None if self.primary_settings is None else self.primary_settings.to_mapping()
            ),
            "supervised_class_ids": (
                None if self.supervised_class_ids is None else list(self.supervised_class_ids)
            ),
            "secondary_channel_roles": (None if self.secondary_channel_roles is None else dict(self.secondary_channel_roles)),
            "supervised_feature_ids": (
                None if self.supervised_feature_ids is None else list(self.supervised_feature_ids)
            ),
        }


def _secondary_channel_roles(value: object, *, allow_empty: bool) -> dict[str, str]:
    if not isinstance(value, Mapping) or (not value and not allow_empty):
        raise BatchTrainingError("Choose at least one input channel for the secondary classifier")
    return {_text(key, "channel role"): _text(channel, "channel ID") for key, channel in value.items()}


def _selection_ids(value: object, field: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BatchTrainingError(f"{field} must be an array or null")
    return tuple(value)  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class TrainingPlanSelectionResponse:
    schema_version: str
    selections: tuple[TrainingPlanCandidateSelection, ...]
    saved_classifier: Mapping[str, str] | None = None
    skip_primary: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != TRAINING_PLAN_SELECTION_SCHEMA_VERSION:
            raise BatchTrainingError("training plan selection schema version is unsupported")
        selections = tuple(self.selections)
        if type(self.skip_primary) is not bool:
            raise BatchTrainingError("Primary classification choice must be explicit")
        if self.skip_primary:
            if selections or self.saved_classifier is not None:
                raise BatchTrainingError("Skip primary classification without choosing a classifier")
            object.__setattr__(self, "selections", ())
            return
        if self.saved_classifier is not None:
            row = _closed(self.saved_classifier, frozenset({"export_path", "export_sha256"}), "saved classifier")
            from pathlib import Path
            from ._shared import _sha256
            path = _text(row["export_path"], "saved classifier export")
            if selections or not Path(path).is_absolute() or Path(path).name != "export.json":
                raise BatchTrainingError("Choose one saved classifier export without training selections")
            _sha256(row["export_sha256"], "saved classifier export digest")
            object.__setattr__(self, "saved_classifier", MappingProxyType(dict(row)))
            object.__setattr__(self, "selections", ())
            return
        if (
            not selections
            or not all(type(item) is TrainingPlanCandidateSelection for item in selections)
            or selections != tuple(sorted(selections, key=lambda item: item.candidate_id))
            or len({item.candidate_id for item in selections}) != len(selections)
        ):
            raise BatchTrainingError("training plan selections must be canonical and unique")
        object.__setattr__(self, "selections", selections)

    @classmethod
    def from_mapping(cls, value: object) -> "TrainingPlanSelectionResponse":
        if isinstance(value, Mapping) and "skip_primary" in value:
            row = _closed(value, frozenset({"schema_version", "skip_primary"}), "primary classification choice")
            if row["skip_primary"] is not True:
                raise BatchTrainingError("Skipping primary classification requires an explicit choice")
            return cls(str(row["schema_version"]), (), skip_primary=True)
        if isinstance(value, Mapping) and "saved_classifier" in value:
            row = _closed(value, frozenset({"schema_version", "saved_classifier"}), "saved classifier response")
            return cls(str(row["schema_version"]), (), row["saved_classifier"])
        row = _closed(
            value,
            frozenset({"schema_version", "selections"}),
            "training plan selection response",
        )
        raw = row["selections"]
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise BatchTrainingError("selections must be an array")
        return cls(
            schema_version=str(row["schema_version"]),
            selections=tuple(TrainingPlanCandidateSelection.from_mapping(item) for item in raw),
        )

    def to_mapping(self) -> dict[str, object]:
        if self.skip_primary:
            return {"schema_version": self.schema_version, "skip_primary": True}
        if self.saved_classifier is not None:
            return {"schema_version": self.schema_version, "saved_classifier": dict(self.saved_classifier)}
        return {
            "schema_version": self.schema_version,
            "selections": [item.to_mapping() for item in self.selections],
        }


def validate_training_plan_selection_response(
    value: object,
    *,
    candidate_entry_ids: Mapping[str, tuple[str, ...]],
    supervised: bool | None = None,
    allow_skip_primary: bool = False,
) -> TrainingPlanSelectionResponse:
    """Validate user choices against authenticated candidate identities.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""

    response = TrainingPlanSelectionResponse.from_mapping(value)
    if response.skip_primary:
        if not allow_skip_primary or supervised is True:
            raise BatchTrainingError("Only primary tissue classification can be skipped here")
        return response
    if response.saved_classifier is not None:
        if supervised is True:
            raise BatchTrainingError("Saved pixel models cannot be used for cell classification")
        return response
    expected = {
        _text(candidate_id, "candidate_id"): tuple(sorted(entry_ids))
        for candidate_id, entry_ids in candidate_entry_ids.items()
    }
    if set(expected) != {item.candidate_id for item in response.selections}:
        raise BatchTrainingError("training plan selection candidate coverage differs")
    for selection in response.selections:
        if selection.primary_settings is not None:
            from .primary_policy import primary_ignore_policy

            primary_ignore_policy(selection.primary_settings, selection.model_configuration)
            validate_reference_class_names(
                tuple(item.display_name for item in selection.primary_settings.primary_classes)
            )
        if selection.supervised_class_ids is not None:
            validate_reference_class_names(selection.supervised_class_ids)
        if supervised is True and selection.supervised_class_ids is None:
            raise BatchTrainingError("supervised training requires explicit classes and features")
        if supervised is False and selection.supervised_class_ids is not None:
            raise BatchTrainingError("pixel training forbids supervised classes and features")
        selected = {item.project_entry_id for item in selection.roles}
        if not selected or not selected <= set(expected[selection.candidate_id]):
            raise BatchTrainingError("training plan role selection contains a foreign entry")
        if (
            selection.evaluation_intent is EvaluationIntent.INDEPENDENT_PERFORMANCE
            and "test" not in {item.split for item in selection.roles}
        ):
            raise BatchTrainingError("independent performance requires a selected test role")
    return response


def apply_primary_ignore_policy(
    settings: PrimaryClassifierSettings,
    model_configuration: Mapping[str, object],
    *,
    policy: str,
    boundary_thickness_px: float = 0.0,
) -> tuple[PrimaryClassifierSettings, dict[str, object]]:
    """Apply an explicit Ignore choice while retaining the selected model and inputs.

    Annotated Ignore joins the reference classes. Boundary Ignore is generated by
    QuPath from the reference edges and receives a derived native class code. We
    preserve biological class codes, estimator parameters and every search candidate.
    """
    import math
    from .primary_policy import primary_ignore_policy

    if type(settings) is not PrimaryClassifierSettings or policy not in {
        "none", "annotated", "boundary"
    }:
        raise BatchTrainingError("Choose how the classifier should identify excluded areas.")
    if len(settings.primary_classes) == 1 and policy != "annotated":
        raise BatchTrainingError("A single class requires Ignore learned from annotated regions.")
    if (
        type(boundary_thickness_px) not in (int, float)
        or not math.isfinite(boundary_thickness_px)
        or (boundary_thickness_px <= 0 if policy == "boundary" else boundary_thickness_px != 0)
    ):
        raise BatchTrainingError("Boundary Ignore requires a positive boundary width in pixels.")
    settings_mapping = settings.to_mapping()
    biological = tuple(
        class_id for class_id in settings.primary_classifier.output_classes if class_id != "Ignore"
    )
    settings_mapping["primary_classifier"]["output_classes"] = list(biological) + (
        [] if policy == "none" else ["Ignore"]
    )
    settings_mapping["primary_classifier"]["require_ignore_training"] = policy == "annotated"
    selected_settings = PrimaryClassifierSettings.from_mapping(settings_mapping)
    configuration = _export_json(model_configuration)

    def update(candidate: dict[str, object]) -> None:
        if "candidates" in candidate:
            for nested in candidate["candidates"]:
                update(nested)
            return
        native = candidate.get("native_configuration", candidate)
        labels = native["output_labels"]["ordered_labels"]
        selected = [dict(row) for row in labels if row["canonical_class_id"] in biological]
        if {row["canonical_class_id"] for row in selected} != set(biological):
            raise BatchTrainingError("The model labels do not cover the biological classes.")
        if policy != "none":
            existing = next((row for row in labels if row["canonical_class_id"] == "Ignore"), None)
            selected.append(dict(existing) if existing is not None else {
                "canonical_class_id": "Ignore",
                "qupath_label": max(row["qupath_label"] for row in selected) + 1,
            })
        native["output_labels"]["ordered_labels"] = selected
        native["boundary_strategy"] = "classify" if policy == "boundary" else "skip"
        native["boundary_thickness_px"] = float(boundary_thickness_px)
        if policy == "boundary":
            native["boundary_class_id"] = selected[-1]["qupath_label"]
        else:
            native.pop("boundary_class_id", None)

    update(configuration)
    primary_ignore_policy(selected_settings, configuration)
    return selected_settings, configuration


def classifier_estimator_settings(configuration: Mapping[str, object]) -> dict[str, object]:
    """Project the supported estimator parameters without image-processing controls."""

    from .qupath_rtrees import QuPathRTreesParameters
    from .qupath_svm import QuPathSvmParameters
    from .qupath_logistic_regression import QuPathLogisticRegressionParameters

    routes = {
        "qupath-pixel-rtrees/v1": ("rtrees", QuPathRTreesParameters),
        "qupath-object-rtrees/v1": ("rtrees", QuPathRTreesParameters),
        "qupath-pixel-svm/v1": ("svm", QuPathSvmParameters),
        "qupath-object-svm/v1": ("svm", QuPathSvmParameters),
        "qupath-object-logistic-regression/v1": ("logistic_regression", QuPathLogisticRegressionParameters),
        "qupath-kronos-rtrees/v2": ("rtrees", QuPathRTreesParameters),
        "qupath-kronos-svm/v2": ("svm", QuPathSvmParameters),
    }
    schema = configuration.get("configuration_schema")
    route = routes.get(schema) if isinstance(schema, str) else None
    if route is None:
        raise BatchTrainingError("Settings changes are unavailable for this classifier method.")
    field, parser = route
    estimator = configuration.get("native_configuration", configuration)
    return {
        "configuration_schema": schema,
        field: parser.from_mapping(estimator.get(field)).to_mapping(),
    }


def validate_training_settings_change_response(
    value: object,
    *,
    expected_review_sha256: str,
    model_configurations: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """Validate estimator-only edits against configurations reopened by the owner.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    from ._shared import _sha256

    row = _closed(
        value,
        frozenset(
            {
                "schema_version",
                "fit_review_sha256",
                "parameters_by_classifier_id",
            }
        ),
        "classifier settings change",
    )
    expected = _sha256(expected_review_sha256, "expected_review_sha256")
    if (
        row["schema_version"] != "classifier-settings-change/v1"
        or row["fit_review_sha256"] != expected
    ):
        raise BatchTrainingError(
            "The classifier review has changed. Reopen Reviews before changing settings."
        )
    changes = row["parameters_by_classifier_id"]
    if (
        not isinstance(changes, Mapping)
        or not changes
        or any(type(key) is not str for key in changes)
        or not set(changes) <= set(model_configurations)
    ):
        raise BatchTrainingError("Choose settings for a classifier shown in this review.")
    checked = {}
    for classifier_id, raw in sorted(changes.items()):
        configuration = classifier_estimator_settings(model_configurations[classifier_id])
        field = next(name for name in ("svm", "rtrees", "logistic_regression") if name in configuration)
        parameters = classifier_estimator_settings({**configuration, field: raw})[field]
        if parameters == configuration[field]:
            raise BatchTrainingError("Change at least one setting for each selected classifier.")
        checked[classifier_id] = parameters
    return {
        "schema_version": "classifier-settings-change/v1",
        "fit_review_sha256": expected,
        "parameters_by_classifier_id": checked,
    }


def selection_for_training_settings_revision(
    plan_set: "TrainingPlanSet",
    change: Mapping[str, object],
    *,
    candidate_entry_ids: Mapping[str, tuple[str, ...]],
    supervised_options: Mapping[str, object] | None,
    measurement_features: tuple["FeatureSpec", ...] | None = None,
) -> TrainingPlanSelectionResponse:
    """Reconstruct only the choices already fixed by an authenticated plan set."""

    from ...cell_analysis.clustering import FeatureSpec
    from ...cell_analysis.measurement_plan import CellFeatureError, MeasurementPlan
    from ._shared import ClassifierKind
    from .planning import DEFERRED_PRIMARY_CANDIDATE_ID, TrainingPlanSet

    if type(plan_set) is not TrainingPlanSet:
        raise BatchTrainingError("settings revision requires a typed training plan set")
    requests = {plan.request.classifier_id: plan.request for plan in plan_set.plans}
    if len(requests) != len(plan_set.plans):
        raise BatchTrainingError("settings revision classifier IDs are ambiguous")
    expected_review = change.get("fit_review_sha256") if isinstance(change, Mapping) else None
    output_amendment = "output_policy_by_classifier_id" in change
    if output_amendment:
        from .output_review import build_output_policy_review, validate_output_policy_change

        review = build_output_policy_review(plan_set)
        if review is None:
            raise BatchTrainingError("This classifier plan does not need an output amendment.")
        normalized = validate_output_policy_change(change, review=review)
    else:
        normalized = validate_training_settings_change_response(
            change,
            expected_review_sha256=expected_review,
            model_configurations={
                classifier_id: request.model_configuration
                for classifier_id, request in requests.items()
            },
        )
    kind = plan_set.classifier_kind
    if kind is ClassifierKind.PRIMARY_COMPARTMENT:
        if len(requests) != 1 or len(candidate_entry_ids) != 1:
            raise BatchTrainingError("primary settings revision candidate is ambiguous")
        candidate_id = next(iter(candidate_entry_ids))
        classifier_id = next(iter(requests))
        if candidate_id not in {classifier_id, DEFERRED_PRIMARY_CANDIDATE_ID}:
            raise BatchTrainingError("primary settings revision candidate differs")
        candidate_by_classifier = {classifier_id: candidate_id}
    elif kind is ClassifierKind.SECONDARY_STRUCTURE:
        if set(candidate_entry_ids) != set(requests):
            raise BatchTrainingError("secondary settings revision candidates differ")
        candidate_by_classifier = {classifier_id: classifier_id for classifier_id in requests}
    elif kind is ClassifierKind.SUPERVISED_PHENOTYPE:
        if set(requests) != {"supervised-phenotype"} or set(candidate_entry_ids) != {
            "supervised-phenotype"
        }:
            raise BatchTrainingError("supervised settings revision candidate differs")
        candidate_by_classifier = {"supervised-phenotype": "supervised-phenotype"}
    else:
        raise BatchTrainingError("settings revision classifier kind is unsupported")

    selected_feature_ids: tuple[str, ...] | None = None
    if kind is ClassifierKind.SUPERVISED_PHENOTYPE:
        if (
            not isinstance(supervised_options, Mapping)
            or type(measurement_features) is not tuple
            or not measurement_features
            or any(type(item) is not FeatureSpec for item in measurement_features)
        ):
            raise BatchTrainingError("supervised settings revision lacks exact measurement owners")
        options = _closed(
            supervised_options,
            frozenset({"configured_class_ids", "available_features"}) | (set(supervised_options) & {"available_channels", "recommended_pixel_size_um", "nuclear_channel_id"}),
            "supervised settings options",
        )
        try:
            owner_rows = MeasurementPlan(measurement_features).to_mapping()["features"]
        except CellFeatureError as exc:
            raise BatchTrainingError(
                "supervised settings revision measurement owners are invalid"
            ) from exc
        if options["available_features"] != owner_rows:
            raise BatchTrainingError("supervised settings options differ from measurement owners")
        request = next(iter(requests.values()))
        configured_classes = options["configured_class_ids"]
        if configured_classes is not None and configured_classes != list(request.required_classes):
            raise BatchTrainingError("supervised settings classes differ from prior plan")
        requirements = request.feature_requirements
        if not isinstance(requirements, Mapping) or set(requirements) != {
            "feature_schema",
            "point_selection_radius_level0",
        }:
            raise BatchTrainingError("supervised settings feature policy is incomplete")
        schema = requirements["feature_schema"]
        radius = requirements["point_selection_radius_level0"]
        if (
            not isinstance(schema, (tuple, list))
            or not schema
            or type(radius) not in (int, float)
            or radius <= 0
        ):
            raise BatchTrainingError("supervised settings feature policy is invalid")
        from .supervised_features import supervised_feature_source, require_kronos_cell_schema, require_kronos_cell_channels
        source = supervised_feature_source(request.model_configuration)
        if source is not None:
            require_kronos_cell_schema(source, schema)
            require_kronos_cell_channels(source, options.get("available_channels"))
            selected_feature_ids = tuple(row["measurement_name"] for row in schema)
        else:
            by_identity: dict[tuple[str, str], str] = {}
            for feature in measurement_features:
                identity = (feature.measurement, feature.role.value)
                if identity in by_identity or feature.feature_id is None:
                    raise BatchTrainingError("supervised settings feature identity is ambiguous")
                by_identity[identity] = feature.feature_id
            selected = []
            for raw in schema:
                row = _closed(
                    raw, frozenset({"measurement_name", "semantic_role"}), "supervised feature"
                )
                identity = (row["measurement_name"], row["semantic_role"])
                feature_id = by_identity.get(identity)
                if feature_id is None or feature_id in selected:
                    raise BatchTrainingError("supervised settings feature differs from prior plan")
                selected.append(feature_id)
            selected_feature_ids = tuple(selected)
    elif supervised_options is not None or measurement_features is not None:
        raise BatchTrainingError("pixel settings revision forbids supervised options")

    selections = []
    for classifier_id, request in sorted(
        requests.items(), key=lambda item: candidate_by_classifier[item[0]]
    ):
        configuration = dict(request.model_configuration)
        selected_settings = request.primary_settings
        if output_amendment:
            choice = normalized["output_policy_by_classifier_id"][classifier_id]
            selected_settings, configuration = apply_primary_ignore_policy(
                selected_settings, configuration, policy=choice["policy"],
                boundary_thickness_px=choice["boundary_thickness_px"],
            )
        parameters = normalized.get("parameters_by_classifier_id", {}).get(classifier_id)
        if parameters is not None:
            schema = configuration.get("configuration_schema")
            field = next(name for name in ("svm", "rtrees", "logistic_regression") if name in classifier_estimator_settings(configuration))
            if schema in {"qupath-kronos-rtrees/v2", "qupath-kronos-svm/v2"}:
                configuration["native_configuration"] = {
                    **configuration["native_configuration"],
                    field: parameters,
                }
            else:
                configuration[field] = parameters
        if kind is ClassifierKind.SUPERVISED_PHENOTYPE:
            configuration["point_selection_radius_level0"] = request.feature_requirements[
                "point_selection_radius_level0"
            ]
        selections.append(
            TrainingPlanCandidateSelection(
                candidate_id=candidate_by_classifier[classifier_id],
                roles=tuple(
                    sorted(
                        (
                            TrainingPlanRoleSelection(item.project_entry_id, item.split)
                            for item in request.entries
                        ),
                        key=lambda item: (item.project_entry_id, item.split),
                    )
                ),
                evaluation_intent=request.evaluation_intent,
                model_configuration=configuration,
                random_seed=request.random_seed,
                primary_settings=selected_settings,
                supervised_class_ids=(
                    request.required_classes
                    if kind is ClassifierKind.SUPERVISED_PHENOTYPE
                    else None
                ),
                supervised_feature_ids=selected_feature_ids,
                secondary_channel_roles=(dict(request.channel_requirements["channel_roles"]) if kind is ClassifierKind.SECONDARY_STRUCTURE else None),
            )
        )
    response = TrainingPlanSelectionResponse(
        schema_version=TRAINING_PLAN_SELECTION_SCHEMA_VERSION,
        selections=tuple(selections),
    )
    return validate_training_plan_selection_response(
        response.to_mapping(),
        candidate_entry_ids=candidate_entry_ids,
        supervised=kind is ClassifierKind.SUPERVISED_PHENOTYPE,
    )


def validate_training_plan_selection_request(value: object) -> dict[str, object]:
    """Normalize the closed review display grammar; source authority is checked by its
    owner.

    The required mapping shape includes ``schema_version``, ``candidate_entry_ids``,
    ``validation_domain_id``, ``candidate_participants``,
    ``primary_settings_by_candidate_id``, ``application_participants``. Field checks are
    applied before the validated value is returned or passed to its next constructor."""

    from ..batch_artifacts import AnalysisDomainBinding, BatchArtifactError
    from .planning import DEFERRED_PRIMARY_CANDIDATE_ID, TrainingPlanApplicationEntry

    fields = {
        "schema_version",
        "candidate_entry_ids",
        "validation_domain_id",
        "candidate_participants",
        "primary_settings_by_candidate_id",
        "application_participants",
        "supervised_options",
        "secondary_output_classes_by_candidate_id",
    }
    fields.add("available_channels_by_candidate_id")
    if isinstance(value, Mapping) and "channel_indices_by_candidate_id" in value:
        fields.add("channel_indices_by_candidate_id")
    if isinstance(value, Mapping) and "nuclear_channel_by_candidate_id" in value:
        fields.add("nuclear_channel_by_candidate_id")
    if isinstance(value, Mapping) and "kronos_intensity_divisor_by_candidate_id" in value:
        fields.add("kronos_intensity_divisor_by_candidate_id")
    if isinstance(value, Mapping) and "original_pixel_size_um_by_entry_id" in value:
        fields.add("original_pixel_size_um_by_entry_id")
    row = _closed(value, frozenset(fields), "training plan selection request")
    if row["schema_version"] != TRAINING_PLAN_SELECTION_REQUEST_SCHEMA_VERSION:
        raise BatchTrainingError("This classifier request requires the current prototype format.")
    domain_id = _text(row["validation_domain_id"], "validation_domain_id")
    candidates = row["candidate_entry_ids"]
    if not isinstance(candidates, Mapping) or not 1 <= len(candidates) <= 128:
        raise BatchTrainingError("candidate_entry_ids must contain bounded candidates")
    candidate_rows: dict[str, list[str]] = {}
    total_entries = 0
    for key, value in candidates.items():
        candidate_id = _text(key, "candidate_id")
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise BatchTrainingError("candidate entry IDs must be an array")
        entry_ids = tuple(_text(item, "project_entry_id") for item in value)
        if not entry_ids or entry_ids != tuple(sorted(set(entry_ids))):
            raise BatchTrainingError("candidate entry IDs must be canonical and unique")
        total_entries += len(entry_ids)
        candidate_rows[candidate_id] = list(entry_ids)
    if total_entries > 512 or list(candidate_rows) != sorted(candidate_rows):
        raise BatchTrainingError("candidate entry inventory is noncanonical or unbounded")
    candidate_ids = {entry_id for entries in candidate_rows.values() for entry_id in entries}
    raw_candidate_participants = row["candidate_participants"]
    if not isinstance(raw_candidate_participants, Mapping) or tuple(
        raw_candidate_participants
    ) != tuple(sorted(candidate_ids)):
        raise BatchTrainingError("candidate participant coverage is noncanonical")
    candidate_participants = {}
    for entry_id, raw_participant in raw_candidate_participants.items():
        participant = _closed(
            raw_participant,
            frozenset({"sample_id", "subject_id"}),
            "candidate participant",
        )
        candidate_participants[entry_id] = {
            "sample_id": _text(participant["sample_id"], "sample_id"),
            "subject_id": _text(participant["subject_id"], "subject_id"),
        }
    if len({item["sample_id"] for item in candidate_participants.values()}) != len(
        candidate_participants
    ):
        raise BatchTrainingError("candidate samples must be unique")
    seeds = row["primary_settings_by_candidate_id"]
    raw_participants = row["application_participants"]
    if isinstance(raw_participants, (str, bytes)) or not isinstance(raw_participants, Sequence):
        raise BatchTrainingError("application_participants must be an array")
    if len(raw_participants) > 512:
        raise BatchTrainingError("application participant inventory is unbounded")
    participants = []
    for raw in raw_participants:
        participant = _closed(
            raw,
            frozenset(
                {
                    "project_entry_id",
                    "sample_id",
                    "slide_id",
                    "subject_id",
                    "image_identity_sha256",
                    "channels",
                    "analysis_domain_bindings",
                }
            ),
            "application participant",
        )
        try:
            entry = TrainingPlanApplicationEntry.from_mapping(
                {key: participant[key] for key in TrainingPlanApplicationEntry.__dataclass_fields__}
            )
            bindings = tuple(
                AnalysisDomainBinding.from_mapping(item)
                for item in participant["analysis_domain_bindings"]
            )
        except (BatchTrainingError, BatchArtifactError, TypeError, ValueError) as exc:
            raise BatchTrainingError("application participant is invalid") from exc
        if not bindings or bindings != tuple(
            sorted(
                bindings,
                key=lambda item: (
                    item.project_entry_id,
                    item.sample_id,
                    item.slide_id,
                    item.analysis_domain_id,
                    item.analysis_domain_revision,
                    item.analysis_domain_revision_sha256,
                    item.domain_set_sha256,
                ),
            )
        ):
            raise BatchTrainingError("application participant domains must be canonical")
        if any(
            (item.project_entry_id, item.sample_id, item.slide_id)
            != (entry.project_entry_id, entry.sample_id, entry.slide_id)
            for item in bindings
        ) or len(set(bindings)) != len(bindings):
            raise BatchTrainingError("application participant domain ownership is invalid")
        participants.append(
            {
                **entry.to_mapping(),
                "analysis_domain_bindings": [item.to_mapping() for item in bindings],
            }
        )
    participant_ids = tuple(item["project_entry_id"] for item in participants)
    if participant_ids != tuple(sorted(set(participant_ids))) or len(
        {item["sample_id"] for item in participants}
    ) != len(participants):
        raise BatchTrainingError("application participant inventory must be canonical and unique")
    if any(
        item["project_entry_id"] not in candidate_participants
        or candidate_participants[item["project_entry_id"]]
        != {"sample_id": item["sample_id"], "subject_id": item["subject_id"]}
        for item in participants
    ):
        raise BatchTrainingError("candidate participants differ from primary application")
    if seeds == {}:
        if participants:
            raise BatchTrainingError("non-primary request cannot carry application participants")
        seed_rows = {}
    else:
        if not isinstance(seeds, Mapping) or set(seeds) != set(candidate_rows) or len(seeds) != 1:
            raise BatchTrainingError("primary seed inventory differs from candidates")
        if not participants or any(
            tuple(values) != participant_ids for values in candidate_rows.values()
        ):
            raise BatchTrainingError("primary application members differ from candidate inventory")
        seed_rows = {}
        for candidate_id in sorted(seeds):
            raw_seed = seeds[candidate_id]
            try:
                settings = (
                    None if raw_seed is None else PrimaryClassifierSettings.from_mapping(raw_seed)
                )
            except ValueError as exc:
                raise BatchTrainingError("primary settings seed is invalid") from exc
            if settings is None and candidate_id != DEFERRED_PRIMARY_CANDIDATE_ID:
                raise BatchTrainingError("deferred primary seed requires owner slot")
            if settings is not None and settings.primary_classifier.classifier_id != candidate_id:
                raise BatchTrainingError("configured primary seed identity differs")
            if (
                settings is not None
                and settings.primary_classifier.validation_domain_id != domain_id
            ):
                raise BatchTrainingError("primary seed validation domain differs")
            seed_rows[candidate_id] = None if settings is None else settings.to_mapping()
    raw_options = row["supervised_options"]
    if raw_options is None:
        if "supervised-phenotype" in candidate_rows:
            raise BatchTrainingError("supervised request lacks feature and class options")
        supervised_options = None
    else:
        if set(candidate_rows) != {"supervised-phenotype"} or seed_rows or participants:
            raise BatchTrainingError("supervised options require the supervised candidate only")
        options = _closed(
            raw_options,
            frozenset({"configured_class_ids", "available_features"}) | (set(raw_options) & {"available_channels", "recommended_pixel_size_um", "nuclear_channel_id"} if isinstance(raw_options, Mapping) else set()),
            "supervised options",
        )
        configured = _selection_ids(options["configured_class_ids"], "configured_class_ids")
        if configured is not None:
            configured = tuple(_text(item, "configured_class_ids") for item in configured)
            if not configured or configured != tuple(sorted(set(configured))):
                raise BatchTrainingError("configured_class_ids must be canonical and unique")
        features = options["available_features"]
        if (
            isinstance(features, (str, bytes))
            or not isinstance(features, Sequence)
            or not 0 < len(features) <= 512
        ):
            raise BatchTrainingError("available_features must be a nonempty bounded array")
        feature_fields = frozenset(
            {
                "feature_id",
                "measurement_name",
                "kind",
                "channel_id",
                "biomarker",
                "compartment",
                "units",
                "producer",
                "producer_version",
                "missing_rate",
                "non_finite_rate",
                "opt_in",
            }
        )
        feature_rows = []
        for raw_feature in features:
            feature = _closed(raw_feature, feature_fields, "available feature")
            for field in ("feature_id", "measurement_name", "kind", "producer", "producer_version"):
                _text(feature[field], field)
            for field in ("channel_id", "biomarker", "compartment", "units"):
                if feature[field] is not None:
                    _text(feature[field], field)
            for field in ("missing_rate", "non_finite_rate"):
                number = feature[field]
                if type(number) not in (int, float) or not 0 <= number <= 1:
                    raise BatchTrainingError(f"available feature {field} is invalid")
            if type(feature["opt_in"]) is not bool:
                raise BatchTrainingError("available feature opt_in is invalid")
            feature_rows.append(dict(feature))
        if len({item["feature_id"] for item in feature_rows}) != len(feature_rows):
            raise BatchTrainingError("available feature IDs must be unique")
        if len({item["measurement_name"] for item in feature_rows}) != len(feature_rows):
            raise BatchTrainingError("available measurement names must be unique")
        supervised_options = {
            "configured_class_ids": None if configured is None else list(configured),
            "available_features": feature_rows,
        }
        if "recommended_pixel_size_um" in options:
            sampling = options["recommended_pixel_size_um"]
            if (not isinstance(sampling, (tuple, list)) or len(sampling) != 2
                    or any(type(value) not in (int, float) or not math.isfinite(value)
                           or value <= 0 for value in sampling)):
                raise BatchTrainingError("Recommended image sampling requires positive X and Y pixel sizes")
            supervised_options["recommended_pixel_size_um"] = [float(value) for value in sampling]
        if "available_channels" in options:
            from mif_pipeline.pipeline_core.config import ChannelSpec
            channels = options["available_channels"]
            if not isinstance(channels, (tuple, list)) or len(channels) > 256:
                raise BatchTrainingError("supervised channels must be a bounded array")
            parsed = tuple(ChannelSpec.from_mapping(item, field="supervised channel") for item in channels)
            if len({item.channel_id for item in parsed}) != len(parsed) or len({item.name for item in parsed}) != len(parsed):
                raise BatchTrainingError("supervised channels must have unique ids and names")
            supervised_options["available_channels"] = [item.to_mapping() for item in parsed]
        if "nuclear_channel_id" in options:
            nuclear = options["nuclear_channel_id"]
            offered = {item["channel_id"]: item for item in supervised_options.get("available_channels", ())}
            if nuclear not in offered or offered[nuclear]["biomarker"] is not None:
                raise BatchTrainingError("Review the nuclear stain in Channels and markers")
            supervised_options["nuclear_channel_id"] = nuclear
    raw_secondary = row["secondary_output_classes_by_candidate_id"]
    if not isinstance(raw_secondary, Mapping) or any(type(key) is not str for key in raw_secondary):
        raise BatchTrainingError("secondary output class inventory must be an object")
    if seed_rows or supervised_options is not None:
        if raw_secondary:
            raise BatchTrainingError("non-secondary request carries secondary output classes")
        secondary_rows: dict[str, object] = {}
    else:
        if tuple(raw_secondary) != tuple(candidate_rows):
            raise BatchTrainingError("secondary output class candidate coverage differs")
        secondary_rows = {}
        for candidate_id, raw_record in raw_secondary.items():
            record = _closed(
                raw_record,
                frozenset(
                    {
                        "structures",
                        "channel_roles",
                        "eligible_parents",
                        "output_classes",
                    }
                ),
                "secondary output class record",
            )
            structures = record["structures"]
            if not isinstance(structures, (list, tuple)) or not structures:
                raise BatchTrainingError("Secondary structures are missing")
            structure_rows = []
            for item in structures:
                item = _closed(item, frozenset({"structure_id", "display_name"}), "secondary structure")
                structure_rows.append({key: _text(value, key) for key, value in item.items()})
            ids = tuple(item["structure_id"] for item in structure_rows)
            if len(set(ids)) != len(ids):
                raise BatchTrainingError("Secondary structures are repeated")
            channel_roles = _secondary_channel_roles(record["channel_roles"], allow_empty=True)
            parents = record["eligible_parents"]
            if not isinstance(parents, (tuple, list)) or not parents:
                raise BatchTrainingError("Secondary classifier permitted parents are missing")
            parent_rows = []
            for parent in parents:
                parent = _closed(parent, frozenset({"parent_id", "display_name"}), "secondary parent")
                parent_rows.append({key: _text(parent[key], f"secondary parent {key}") for key in parent})
            if len({parent["parent_id"] for parent in parent_rows}) != len(parent_rows):
                raise BatchTrainingError("Secondary classifier permitted parents are repeated")
            classes = record["output_classes"]
            if not isinstance(classes, (list, tuple)) or not classes:
                raise BatchTrainingError("Secondary output classes are missing")
            class_rows = []
            for raw_class in classes:
                item = _closed(raw_class, frozenset({"canonical_class_id", "display_name"}), "secondary output class")
                class_id = _text(item["canonical_class_id"], "canonical_class_id")
                if item["display_name"] != class_id:
                    raise BatchTrainingError("Secondary output class display differs from its annotation label")
                class_rows.append(dict(item))
            labels = [item["canonical_class_id"] for item in class_rows]
            if len(set(labels)) != len(labels) or {label for label in labels if label.startswith("Candidate_")} != {f"Candidate_{item}" for item in ids}:
                raise BatchTrainingError("Secondary output classes differ from structure labels")
            secondary_rows[candidate_id] = {
                "structures": structure_rows,
                "channel_roles": channel_roles,
                "eligible_parents": parent_rows,
                "output_classes": class_rows,
            }
    channels_by_candidate = {}
    from mif_pipeline.pipeline_core.config import ChannelSpec

    inventory = row["available_channels_by_candidate_id"]
    if not isinstance(inventory, Mapping) or set(inventory) != (
        set(candidate_rows) if supervised_options is None else set()
    ):
        raise BatchTrainingError(
            "The offered channel inventory does not match the classifier candidates"
        )
    for candidate_id, values in inventory.items():
        if not isinstance(values, (tuple, list)) or not values or len(values) > 256:
            raise BatchTrainingError(
                "The offered channel inventory must be a bounded nonempty array"
            )
        parsed = tuple(
            ChannelSpec.from_mapping(item, field="classifier channel") for item in values
        )
        if len({item.channel_id for item in parsed}) != len(parsed):
            raise BatchTrainingError("The offered classifier channels are duplicated")
        channel_rows = [item.to_mapping() for item in parsed]
        if seed_rows and any(
            participant["channels"] != channel_rows for participant in participants
        ):
            raise BatchTrainingError(
                "The classifier channel inventory differs from the admitted images"
            )
        if candidate_id in secondary_rows and not set(secondary_rows[candidate_id]["channel_roles"].values()) <= {item.channel_id for item in parsed}:
            raise BatchTrainingError("A saved secondary input channel is absent from the prepared images")
        channels_by_candidate[candidate_id] = channel_rows
    indices_by_candidate = row.get("channel_indices_by_candidate_id", {})
    if not isinstance(indices_by_candidate, Mapping) or set(indices_by_candidate) != set(secondary_rows):
        raise BatchTrainingError("Secondary image-feature previews require the selected channels' source indices")
    for candidate, indices in indices_by_candidate.items():
        if (not isinstance(indices, (list, tuple))
                or len(indices) != len(channels_by_candidate[candidate])
                or any(type(index) is not int or not 0 <= index < 256 for index in indices)
                or list(indices) != sorted(set(indices))):
            raise BatchTrainingError("Secondary channel indices must match the ordered selected channels")
    original_pixel_sizes = {}
    if "original_pixel_size_um_by_entry_id" in row:
        sizes = row["original_pixel_size_um_by_entry_id"]
        if supervised_options is not None or not isinstance(sizes, Mapping) or set(sizes) != candidate_ids:
            raise BatchTrainingError("Original image resolutions require the exact tissue classifier images")
        for entry_id, axes in sizes.items():
            if (not isinstance(axes, (list, tuple)) or len(axes) != 2
                    or any(type(v) not in (int, float) or not 0 < v <= float_info.max for v in axes)):
                raise BatchTrainingError("Original image resolutions require positive finite X and Y pixel sizes")
            original_pixel_sizes[entry_id] = [float(v) for v in axes]
    divisors = row.get("kronos_intensity_divisor_by_candidate_id", {})
    if (not isinstance(divisors, Mapping) or set(divisors) - set(candidate_rows)
            or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                   for value in divisors.values())):
        raise BatchTrainingError("KRONOS intensity defaults must be positive values for offered classifiers")
    nuclear = row.get("nuclear_channel_by_candidate_id", {})
    if not isinstance(nuclear, Mapping) or set(nuclear) - set(channels_by_candidate):
        raise BatchTrainingError("Nuclear-stain choices must belong to the offered classifiers")
    for candidate, channel_id in nuclear.items():
        offered = {item["channel_id"]: item for item in channels_by_candidate[candidate]}
        if channel_id not in offered or offered[channel_id]["biomarker"] is not None:
            raise BatchTrainingError("Review the nuclear stain in Channels and markers")
    return {
        "schema_version": row["schema_version"],
        **({"nuclear_channel_by_candidate_id": dict(nuclear)} if nuclear else {}),
        **({"kronos_intensity_divisor_by_candidate_id": dict(divisors)} if divisors else {}),
        **({"original_pixel_size_um_by_entry_id": original_pixel_sizes} if original_pixel_sizes else {}),
        "available_channels_by_candidate_id": channels_by_candidate,
        **({"channel_indices_by_candidate_id": {key: list(value) for key, value in indices_by_candidate.items()}}
           if indices_by_candidate else {}),
        "candidate_entry_ids": candidate_rows,
        "candidate_participants": candidate_participants,
        "validation_domain_id": domain_id,
        "primary_settings_by_candidate_id": seed_rows,
        "application_participants": participants,
        "supervised_options": supervised_options,
        "secondary_output_classes_by_candidate_id": secondary_rows,
    }


__all__ = [
    "TRAINING_PLAN_SELECTION_SCHEMA_VERSION",
    "TRAINING_PLAN_SELECTION_REQUEST_SCHEMA_VERSION",
    "TrainingPlanCandidateSelection",
    "TrainingPlanRoleSelection",
    "TrainingPlanSelectionResponse",
    "selection_for_training_settings_revision",
    "validate_training_plan_selection_response",
    "validate_training_plan_selection_request",
]
