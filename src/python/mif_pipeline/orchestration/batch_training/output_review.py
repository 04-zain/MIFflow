"""Present an explicit output-policy amendment before native classifier fitting.

We keep the accepted plan as the source of patient roles, image inputs and model
settings. A new selection changes only how Ignore is learned or generated, then
the existing plan-revision owner publishes its successor.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ...pipeline_core.config import PrimaryClassifierSettings
if TYPE_CHECKING:
    from .planning import TrainingPlanSet

from ._shared import BatchTrainingError, ClassifierKind, _closed_mapping, _digest, _export_json, _sha256
from .plan_selection import apply_primary_ignore_policy


def build_output_policy_review(plan_set: TrainingPlanSet) -> dict[str, object] | None:
    """Describe an incoherent primary output choice without claiming a fitted model.

    Retained plans remain readable so their authors can choose a valid successor.
    We offer this review only for the primary Ignore relationship; other model or
    input failures keep their own validation boundary.
    """
    from .primary_policy import primary_ignore_policy

    if plan_set.classifier_kind is not ClassifierKind.PRIMARY_COMPARTMENT:
        return None
    rows = []
    invalid = False
    for plan in plan_set.plans:
        request = plan.request
        try:
            primary_ignore_policy(request.primary_settings, request.model_configuration)
        except BatchTrainingError as error:
            invalid = True
            problem = str(error)
        else:
            problem = None
        rows.append({
            "classifier_id": request.classifier_id,
            "primary_settings": request.primary_settings.to_mapping(),
            "model_configuration": _export_json(request.model_configuration),
            "problem": problem,
            "available_policies": [
                policy for policy in ("none", "annotated")
                if (policy != "annotated" or "Ignore" in request.required_classes)
                and _policy_available(request.primary_settings, request.model_configuration, policy)
            ],
        })
    if not invalid:
        return None
    body = {
        "schema_version": "training-fit-review/v5",
        "phase": "configure_outputs",
        "training_plan_set_sha256": plan_set.training_plan_set_sha256,
        "classifiers": rows,
    }
    return {**body, "fit_review_sha256": _digest(body)}


def _policy_available(
    settings: PrimaryClassifierSettings, configuration: Mapping[str, object], policy: str,
) -> bool:
    """Offer modes supported by this retained estimator and reference set."""
    try:
        apply_primary_ignore_policy(
            settings, configuration, policy=policy,
            boundary_thickness_px=0.0,
        )
    except BatchTrainingError:
        return False
    return True


def validate_output_policy_review(value: object) -> dict[str, object]:
    """Check the closed pre-fit display record and its exact content digest."""
    row = _closed_mapping(value, fields=frozenset({
        "schema_version", "phase", "training_plan_set_sha256", "classifiers", "fit_review_sha256",
    }), field="primary output review")
    if row["schema_version"] != "training-fit-review/v5" or row["phase"] != "configure_outputs":
        raise BatchTrainingError("This request is not a primary output review.")
    _sha256(row["training_plan_set_sha256"], "training_plan_set_sha256")
    _sha256(row["fit_review_sha256"], "fit_review_sha256")
    classifiers = row["classifiers"]
    if not isinstance(classifiers, (list, tuple)) or len(classifiers) != 1:
        raise BatchTrainingError("Primary output review requires one classifier.")
    for raw in classifiers:
        item = _closed_mapping(raw, fields=frozenset({
            "classifier_id", "primary_settings", "model_configuration", "problem", "available_policies",
        }), field="primary output choice")
        settings = PrimaryClassifierSettings.from_mapping(item["primary_settings"])
        if (
            settings.primary_classifier.classifier_id != item["classifier_id"]
            or not isinstance(item["model_configuration"], Mapping)
            or not isinstance(item["problem"], str) or not item["problem"]
            or not isinstance(item["available_policies"], (list, tuple))
            or not item["available_policies"]
            or any(policy not in {"none", "annotated", "boundary"} for policy in item["available_policies"])
        ):
            raise BatchTrainingError("The primary output review is incomplete.")
    body = {key: value for key, value in row.items() if key != "fit_review_sha256"}
    if _digest(body) != row["fit_review_sha256"]:
        raise BatchTrainingError("The output review changed. Reopen Reviews to continue.")
    return _export_json(row)


def validate_output_policy_change(
    value: object, *, review: Mapping[str, object]
) -> dict[str, object]:
    """Bind one explicit output choice to the current reviewed primary plan.

    The model reconstruction validates the selected policy before its response
    can be accepted. The state owner separately verifies the current plan and head.
    """
    checked = validate_output_policy_review(review)
    row = _closed_mapping(value, fields=frozenset({
        "schema_version", "fit_review_sha256", "output_policy_by_classifier_id",
    }), field="primary output amendment")
    if row["schema_version"] != "classifier-settings-change/v1" or row["fit_review_sha256"] != checked["fit_review_sha256"]:
        raise BatchTrainingError("The output review changed. Reopen Reviews to continue.")
    choices = row["output_policy_by_classifier_id"]
    expected = {item["classifier_id"] for item in checked["classifiers"]}
    if not isinstance(choices, Mapping) or set(choices) != expected:
        raise BatchTrainingError("Choose an Ignore policy for the displayed classifier.")
    for item in checked["classifiers"]:
        choice = _closed_mapping(choices[item["classifier_id"]], fields=frozenset({
            "policy", "boundary_thickness_px",
        }), field="Ignore policy")
        if choice["policy"] not in {"none", "annotated"} or choice["policy"] not in item["available_policies"]:
            raise BatchTrainingError("Choose a policy supported by the reviewed reference annotations.")
        apply_primary_ignore_policy(
            PrimaryClassifierSettings.from_mapping(item["primary_settings"]),
            item["model_configuration"], policy=choice["policy"],
            boundary_thickness_px=choice["boundary_thickness_px"],
        )
    return _export_json(row)
