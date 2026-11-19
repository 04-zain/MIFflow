"""Compute subject-grouped folds and classifier-selection scores.

Subject IDs remain intact when constructing folds. For the recorded scoring rule, each
subject's confusion matrix is normalized to unit mass before the matrices are combined and
macro-F1 is calculated. The resulting selection is therefore not determined solely by the
number of labelled pixels in a large image."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from collections.abc import Mapping, Sequence
import hashlib
import math
from typing import Any

CV_SCHEMA = "qupath-grouped-cross-validation/v1"
CV_SCORE = "subject_weighted_macro_f1/v1"
REPORT_SCHEMA = "mif-grouped-cv-selection/v1"
CV_REVIEW_MODEL_MAX_BYTES = 128 * 1024 * 1024


def cv_review_resource_paths(fit_execution, configuration, subjects, seed, native_type="pixel"):
    """Name one separate review model for each fold of the recorded pixel fit."""
    if native_type != "pixel" or configuration.get("configuration_schema") != CV_SCHEMA:
        return ()
    assignment = subject_folds(subjects, configuration["folds"], seed)
    return tuple(f"classifiers/cv_review/mif-{fit_execution}-fold-{fold}.json"
                 for fold in sorted(set(assignment.values())))


def request_cv_review_resource_paths(request):
    """Use the request's development allocation for its exact review outputs."""
    return cv_review_resource_paths(request["fit_execution_sha256"],
        request["model_configuration"],
        [row["entry_state"]["subject_id"] for row in request["training_entries"]]
        if request["native_classifier_type"] == "pixel" else (),
        request["random_seed"], request["native_classifier_type"])


def validate_cv_fold_resources(value, report):
    """Check the compact index against the selected candidate and held-out folds."""
    import re

    if (not isinstance(value, Mapping)
            or set(value) != {"schema", "fit_execution_sha256", "selected_candidate_index", "models"}
            or value["schema"] != "mif-cv-fold-resources/v1"
            or not isinstance(value["fit_execution_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["fit_execution_sha256"]) is None
            or type(value["selected_candidate_index"]) is not int
            or value["selected_candidate_index"] != report["selected_candidate_index"]
            or not isinstance(value["models"], (list, tuple))):
        raise ValueError("CV review resources do not identify the selected fit")
    assignment = report["folds_by_subject"]
    folds = sorted(set(assignment.values()))
    if len(value["models"]) != len(folds):
        raise ValueError("CV review resources do not cover every held-out fold")
    for fold, model in zip(folds, value["models"], strict=True):
        expected_path = f"classifiers/cv_review/mif-{value['fit_execution_sha256']}-fold-{fold}.json"
        if (not isinstance(model, Mapping)
                or set(model) != {"fold", "project_relative_path", "sha256", "byte_count", "held_out_subject_ids"}
                or type(model["fold"]) is not int or model["fold"] != fold
                or model["project_relative_path"] != expected_path
                or not isinstance(model["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", model["sha256"]) is None
                or type(model["byte_count"]) is not int
                or not 1 <= model["byte_count"] <= CV_REVIEW_MODEL_MAX_BYTES
                or not isinstance(model["held_out_subject_ids"], (list, tuple))
                or list(model["held_out_subject_ids"]) != sorted(s for s in assignment if assignment[s] == fold)):
            raise ValueError("CV review model identity, size or held-out subjects differ")
    return value


# ==============================================================================
#                                Model selection
# ==============================================================================


def subject_folds(subject_ids: Sequence[str], folds: int, seed: int) -> dict[str, int]:
    subjects = sorted(set(subject_ids))
    if not 2 <= len(subjects) <= 512 or any(
        type(s) is not str or not s or s != s.strip() for s in subjects
    ):
        raise ValueError(
            "Subject-grouped cross-validation needs 2–512 distinct development subjects"
        )
    if type(folds) is not int or type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError("Fold count and seed must be recorded non-negative integers")
    count = min(5, len(subjects)) if folds == 0 else folds
    if not 2 <= count <= min(10, len(subjects)):
        raise ValueError("Use 2–10 folds, no more than the number of development subjects")
    ranked = sorted(subjects, key=lambda s: (hashlib.sha256(f"{seed}|{s}".encode()).hexdigest(), s))
    return dict(sorted((subject, index % count) for index, subject in enumerate(ranked)))


def _confusion(value: Any, width: int | None = None) -> list[list[int]]:
    if not isinstance(value, (list, tuple)) or not 2 <= len(value) <= 128:
        raise ValueError("A confusion matrix needs its complete ordered class inventory")
    n = len(value)
    if width is not None and n != width:
        raise ValueError("Confusion matrices differ in their class inventory")
    if any(
        not isinstance(row, (list, tuple))
        or len(row) != n
        or any(type(count) is not int or not 0 <= count <= (1 << 53) - 1 for count in row)
        for row in value
    ):
        raise ValueError("Confusion counts must be bounded non-negative integers")
    if sum(sum(row) for row in value) == 0:
        raise ValueError("A held-out subject has no evaluable reference observations")
    return [list(row) for row in value]


def subject_weighted_macro_f1(subject_confusions: Mapping[str, Any]) -> float:
    if not isinstance(subject_confusions, Mapping) or not subject_confusions:
        raise ValueError("Model selection requires held-out subject observations")
    width = None
    pooled = None
    for subject in sorted(subject_confusions):
        matrix = _confusion(subject_confusions[subject], width)
        if width is None:
            width = len(matrix)
            pooled = [[0.0] * width for _ in range(width)]
        total = sum(map(sum, matrix))
        for i in range(width):
            for j in range(width):
                pooled[i][j] += matrix[i][j] / total
    scores = []
    for i in range(width):
        denominator = sum(pooled[i]) + sum(row[i] for row in pooled)
        scores.append(2 * pooled[i][i] / denominator if denominator else 0.0)
    return sum(scores) / len(scores)


def validate_entry_partitions(
    entries: Sequence[Mapping[str, Any]], classes: Sequence[str], folds: int, seed: int
):
    if not 2 <= len(classes) <= 128 or len(set(classes)) != len(classes):
        raise ValueError("Cross-validation requires distinct declared classes")
    if not entries or len({r["entry_id"] for r in entries}) != len(entries):
        raise ValueError("Development image entries must be nonempty and unique")
    assignments = subject_folds([r["subject_id"] for r in entries], folds, seed)
    declared = set(classes)
    if any(not set(r["classes"]) <= declared for r in entries):
        raise ValueError("A reference annotation uses an undeclared class")
    for fold in set(assignments.values()):
        train = [r for r in entries if assignments[r["subject_id"]] != fold]
        if set().union(*(set(r["classes"]) for r in train)) != declared:
            raise ValueError(
                f"Fold {fold+1} training subjects do not cover every declared class. Add representative references or revise the cohort."
            )
    return assignments


def build_selection_report(
    assignments: Mapping[str, int],
    classes: Sequence[str],
    candidate_confusions: Sequence[Mapping[str, Any]],
    seed: int,
) -> dict:
    if not 1 <= len(candidate_confusions) <= 12:
        raise ValueError("Compare 1–12 prespecified parameter candidates")
    rows = []
    supports = None
    subject_ids = sorted(assignments)
    folds = sorted(set(assignments.values()))
    for index, confusions in enumerate(candidate_confusions):
        if set(confusions) != set(subject_ids):
            raise ValueError(
                "Every candidate must evaluate exactly the same development subjects once"
            )
        matrices = {s: _confusion(confusions[s], len(classes)) for s in subject_ids}
        observed = {s: [sum(r) for r in matrices[s]] for s in subject_ids}
        if supports is not None and supports != observed:
            raise ValueError("Candidate comparisons cannot change held-out reference coverage")
        supports = observed
        fold_scores = [
            subject_weighted_macro_f1({s: matrices[s] for s in subject_ids if assignments[s] == f})
            for f in folds
        ]
        rows.append(
            {
                "candidate_index": index,
                "subject_confusions": matrices,
                "fold_scores": fold_scores,
                "selection_score": subject_weighted_macro_f1(matrices),
            }
        )
    selected = max(range(len(rows)), key=lambda i: (rows[i]["selection_score"], -i))
    return {
        "schema": REPORT_SCHEMA,
        "scoring": CV_SCORE,
        "folds_by_subject": dict(sorted(assignments.items())),
        "classes": list(classes),
        "candidates": rows,
        "selected_candidate_index": selected,
        "selection_score": rows[selected]["selection_score"],
        "refit_subject_ids": subject_ids,
        "refit_seed": seed,
        "learned_chain_refit_per_fold": True,
        "score_interpretation": "tuning_not_independent_test",
    }


def validate_selection_report(
    report: Mapping[str, Any], entries, classes, candidates: int, folds: int, seed: int
) -> dict:
    """Recompute reported scores and winning candidate from exact native counts."""
    assignments = validate_entry_partitions(entries, classes, folds, seed)
    try:
        candidate_rows = report["candidates"]
        if len(candidate_rows) != candidates:
            raise ValueError("Model selection report candidate count differs")
        expected = build_selection_report(
            assignments, classes, [r["subject_confusions"] for r in candidate_rows], seed
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("Model selection report is incomplete") from exc

    if "reference_observations" in report:
        expected["reference_observations"] = validate_cv_reference_observations(
            report["reference_observations"], expected, entries)
    if "fold_resources" in report:
        expected["fold_resources"] = validate_cv_fold_resources(report["fold_resources"], expected)
    if "pixel_scores" in report:
        from mif_pipeline.compartment_analysis.precision_recall import validate_cv_pixel_scores
        expected["pixel_scores"] = validate_cv_pixel_scores(report["pixel_scores"], expected, entries)

    def compare(a, b):
        if isinstance(b, dict):
            return (
                isinstance(a, Mapping)
                and set(a) == set(b)
                and all(compare(a[k], v) for k, v in b.items())
            )
        if isinstance(b, list):
            return (
                isinstance(a, (list, tuple))
                and len(a) == len(b)
                and all(compare(x, y) for x, y in zip(a, b))
            )
        if type(b) is float:
            return type(a) in (int, float) and math.isfinite(a) and abs(a - b) <= 1e-10
        return type(a) is type(b) and a == b

    if not compare(report, expected):
        raise ValueError(
            "Model selection report changes the folds, observations, selected settings or full-development refit"
        )
    return expected


def validate_cv_reference_observations(value, report, entries=None):
    """Bind optional original-fold reference memberships to its selected sample counts.

    Memberships may overlap; their sums are not a second confusion matrix.
    """
    if (not isinstance(value, Mapping) or set(value) != {"sample_unit", "membership", "rows"}
            or value["sample_unit"] not in {"pixels", "patches"} or value["membership"] != "sample_center"
            or not isinstance(value["rows"], (list, tuple))):
        raise ValueError("CV reference samples need their unit and centre-membership convention")
    fields = {"project_entry_id", "subject_id", "annotation_uuid", "reference_class",
              "normalized_roi_sha256", "roi_bounds", "declared_label_counts", "fold"}
    classes = report["classes"]
    matrices = report["candidates"][report["selected_candidate_index"]]["subject_confusions"]
    expected = None if entries is None else {
        (entry["entry_id"], entry["subject_id"], annotation): reference
        for entry in entries for annotation, reference in entry.get("references", {}).items()}
    identities = set()
    for row in value["rows"]:
        if not isinstance(row, Mapping) or set(row) != fields:
            raise ValueError("CV reference observation fields differ")
        identity = (row["project_entry_id"], row["subject_id"], row["annotation_uuid"])
        subject, truth = row["subject_id"], row["reference_class"]
        if (any(type(item) is not str or not item for item in identity) or identity in identities
                or subject not in matrices or truth not in classes
                or type(row["fold"]) is not int or row["fold"] != report["folds_by_subject"][subject]):
            raise ValueError("CV reference identity or held-out fold differs")
        identities.add(identity)
        if expected is not None:
            source = expected.get(identity)
            if source is None or source["class_id"] != truth or source["normalized_roi_sha256"] != row["normalized_roi_sha256"]:
                raise ValueError("CV reference differs from the original training request")
        counts = row["declared_label_counts"]
        if (not isinstance(counts, Mapping) or set(counts) != set(classes)
                or any(type(counts[c]) is not int or not 0 <= counts[c] <= matrices[subject][classes.index(truth)][i]
                       for i, c in enumerate(classes))):
            raise ValueError("CV reference votes exceed the selected subject's recorded samples")
        bounds = row["roi_bounds"]
        if (not isinstance(bounds, Mapping) or set(bounds) != {"x", "y", "width", "height", "z", "t"}
                or any(type(bounds[k]) not in (int, float) or not math.isfinite(bounds[k]) for k in ("x", "y", "width", "height"))
                or bounds["width"] <= 0 or bounds["height"] <= 0
                or any(type(bounds[k]) is not int or bounds[k] < 0 for k in ("z", "t"))):
            raise ValueError("CV reference bounds or plane are invalid")
    if expected is not None and identities != set(expected):
        raise ValueError("CV reference observations do not cover the original reference inventory")
    return value


def search_candidates(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Validate search structure/data invariants before base-family validation."""
    if not isinstance(value, Mapping) or set(value) != {
        "configuration_schema",
        "folds",
        "scoring",
        "candidates",
    }:
        raise ValueError("Search configuration fields are incomplete or unknown")
    if value["configuration_schema"] != CV_SCHEMA or value["scoring"] != CV_SCORE:
        raise ValueError("Unsupported grouped search or scoring rule")
    if type(value["folds"]) is not int or value["folds"] not in (0, *range(2, 11)):
        raise ValueError("Search uses automatic or 2–10 grouped folds")
    rows = value["candidates"]
    allowed = {
        "qupath-object-rtrees/v1",
        "qupath-object-svm/v1",
        "qupath-object-logistic-regression/v1",
        "qupath-pixel-rtrees/v1",
        "qupath-pixel-svm/v1",
        "qupath-kronos-rtrees/v2",
        "qupath-kronos-svm/v2",
    }
    if (
        not isinstance(rows, (tuple, list))
        or not 1 <= len(rows) <= 12
        or any(
            not isinstance(r, Mapping) or r.get("configuration_schema") not in allowed for r in rows
        )
    ):
        raise ValueError("Search requires 1–12 supported explicit classifier candidates")
    import json

    def export(item):
        if isinstance(item, Mapping):
            return {k: export(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [export(v) for v in item]
        return item

    signatures = []
    keys = []
    for row in rows:
        item = export(row)
        keys.append(json.dumps(item, sort_keys=True, allow_nan=False))
        if item["configuration_schema"].startswith("qupath-kronos"):
            graph = item.pop("graph", None)
            if not isinstance(graph, dict):
                raise ValueError("KRONOS search candidate lacks graph settings")
            native = item.get("native_configuration")
        else:
            native = item
        if not isinstance(native, dict):
            raise ValueError("Search candidate has no native configuration")
        native.pop("rtrees", None)
        native.pop("svm", None)
        native.pop("logistic_regression", None)
        signatures.append(item)
    if any(s != signatures[0] for s in signatures) or len(set(keys)) != len(keys):
        raise ValueError("Search candidates change data definitions or duplicate settings")
    return tuple(rows)


def request_entries(request: Mapping[str, Any]) -> list[dict]:
    return [
        {
            "entry_id": entry["entry_state"]["project_entry_id"],
            "subject_id": entry["entry_state"]["subject_id"],
            "classes": sorted({r["class_id"] for r in entry["evidence_rows"]}),
            "references": {r["annotation_uuid"]: {"class_id": r["class_id"],
                "normalized_roi_sha256": r["normalized_roi_sha256"]} for r in entry["evidence_rows"]},
        }
        for entry in request["training_entries"]
    ]
