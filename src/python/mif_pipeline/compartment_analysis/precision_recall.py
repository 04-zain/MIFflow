"""Exact one-versus-rest PR from retained held-out native pixel scores.

Equal unrounded native RF scores are grouped with positive/negative counts.
Curves pool pixels, not subjects. AP uses noninterpolated recall increments.
"""
from __future__ import annotations

from collections.abc import Mapping
import math
import re

SCHEMA = "mif-held-out-pixel-scores/v1"
_FIELDS = {"schema", "split", "fit_execution_sha256", "score_kind", "sample_unit",
           "sampling", "class_ids", "entries"}
_ENTRY = {"project_entry_id", "subject_id", "fold", "model_sha256",
          "reference_class_counts", "histograms"}
_MAX_COUNT = (1 << 53) - 1


def _text(value):
    return type(value) is str and value == value.strip() and 0 < len(value) <= 4096


def _sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _array(value):
    return isinstance(value, (list, tuple))


def validate_pixel_score_evidence(value):
    """Validate exact tied-score coverage and return a detached JSON value."""
    if (not isinstance(value, Mapping) or set(value) != _FIELDS
            or value["schema"] != SCHEMA
            or value["split"] not in {"validation", "test", "cross_validation"}
            or not _sha(value["fit_execution_sha256"])
            or value["score_kind"] != "native_tree_vote_fraction"
            or value["sample_unit"] != "pixels"
            or value["sampling"] != "reference_class_union_pixel_centres"):
        raise ValueError("Precision–recall requires retained held-out native pixel scores.")
    classes = value["class_ids"]
    if (not _array(classes) or not 2 <= len(classes) <= 128
            or any(not _text(c) for c in classes) or len(set(classes)) != len(classes)):
        raise ValueError("The score class order is invalid.")
    raw_entries = value["entries"]
    if not _array(raw_entries) or not 1 <= len(raw_entries) <= 4096:
        raise ValueError("The held-out score entries are absent or exceed their bound.")
    entries, identities, folds = [], set(), {}
    for row in raw_entries:
        if (not isinstance(row, Mapping) or set(row) != _ENTRY
                or not _text(row["project_entry_id"]) or not _text(row["subject_id"])
                or not _sha(row["model_sha256"])):
            raise ValueError("A score entry lacks its image, subject or model identity.")
        if row["project_entry_id"] in identities:
            raise ValueError("A held-out image is repeated in the score evidence.")
        identities.add(row["project_entry_id"])
        fold = row["fold"]
        if value["split"] == "cross_validation":
            if type(fold) is not int or not 0 <= fold < 10:
                raise ValueError("CV scores require their original held-out fold.")
            binding = (fold, row["model_sha256"])
            if row["subject_id"] in folds and folds[row["subject_id"]] != binding:
                raise ValueError("One subject cannot change fold or model within a PR export.")
            folds[row["subject_id"]] = binding
        elif fold is not None:
            raise ValueError("Independent validation/test scores cannot claim a CV fold.")
        supports = row["reference_class_counts"]
        if (not _array(supports) or len(supports) != len(classes)
                or any(type(n) is not int or not 0 <= n <= _MAX_COUNT for n in supports)
                or not 0 < sum(supports) <= _MAX_COUNT):
            raise ValueError("The score reference pixel counts are invalid.")
        histograms = row["histograms"]
        if not _array(histograms) or len(histograms) != len(classes):
            raise ValueError("Score histograms must cover every declared class.")
        copied = []
        for support, bins in zip(supports, histograms, strict=True):
            if not _array(bins) or not 1 <= len(bins) <= 100001:
                raise ValueError("The exact score histogram is empty or exceeds its bound.")
            previous, positive, negative, points = -1.0, 0, 0, []
            for item in bins:
                if not _array(item) or len(item) != 3:
                    raise ValueError("Each score bin requires score and positive/negative counts.")
                score, pos, neg = item
                if (type(score) not in (int, float) or not math.isfinite(score)
                        or not previous < score <= 1 or score < 0
                        or type(pos) is not int or type(neg) is not int
                        or not 0 <= pos <= _MAX_COUNT or not 0 <= neg <= _MAX_COUNT
                        or pos + neg == 0):
                    raise ValueError("Scores must be distinct, increasing finite native vote fractions.")
                previous = score; positive += pos; negative += neg
                points.append([score, pos, neg])
            if positive != support or positive + negative != sum(supports):
                raise ValueError("A score histogram does not cover the complete reference pixels.")
            copied.append(points)
        entries.append({**dict(row), "reference_class_counts": list(supports), "histograms": copied})
    return {**dict(value), "class_ids": list(classes), "entries": entries}


def validate_cv_pixel_scores(value, report, entries=None):
    """Bind scores to the selected original fold models and sample supports."""
    data = validate_pixel_score_evidence(value)
    resources = report.get("fold_resources")
    if (data["split"] != "cross_validation" or data["class_ids"] != list(report["classes"])
            or resources is None or data["fit_execution_sha256"] != resources["fit_execution_sha256"]):
        raise ValueError("CV scores differ from the selected fitted model inventory.")
    models = {r["fold"]: r for r in resources["models"]}
    expected = None if entries is None else {r["entry_id"]: r["subject_id"] for r in entries}
    if expected is not None and {r["project_entry_id"]: r["subject_id"] for r in data["entries"]} != expected:
        raise ValueError("CV scores do not cover the held-out image entries exactly.")
    supports = {}
    for row in data["entries"]:
        subject, fold = row["subject_id"], row["fold"]
        if (report["folds_by_subject"].get(subject) != fold or fold not in models
                or models[fold]["sha256"] != row["model_sha256"]
                or subject not in models[fold]["held_out_subject_ids"]):
            raise ValueError("CV score subject, original fold or model digest differs.")
        totals = supports.setdefault(subject, [0] * len(data["class_ids"]))
        for i, n in enumerate(row["reference_class_counts"]):
            totals[i] += n
    matrices = report["candidates"][report["selected_candidate_index"]]["subject_confusions"]
    if supports != {s: [sum(r) for r in matrix] for s, matrix in matrices.items()}:
        raise ValueError("CV score pixel supports differ from the original held-out confusion counts.")
    return data


def validate_held_out_pixel_scores(value, request):
    """Require exact held-out request coverage and the unchanged fitted model."""
    if not _array(value) or not value or request["native_classifier_type"] != "pixel":
        raise ValueError("Native pixel scores require nonempty held-out tissue evaluation.")
    datasets = [validate_pixel_score_evidence(item) for item in value]
    expected = {r["project_entry_id"]: r for r in request["evaluation_entries"]}
    seen, splits = set(), set()
    for data in datasets:
        if (data["split"] in splits or data["split"] not in {"validation", "test"}
                or data["class_ids"] != list(request["declared_class_order"])
                or data["fit_execution_sha256"] != request["fit_execution_sha256"]):
            raise ValueError("Held-out scores changed the evaluated split, fit or class order.")
        splits.add(data["split"])
        for row in data["entries"]:
            source = expected.get(row["project_entry_id"])
            if (source is None or row["project_entry_id"] in seen
                    or row["subject_id"] != source["subject_id"] or source["split"] != data["split"]
                    or row["model_sha256"] != request["native_classifier_sha256"]
                    or {c for c, n in zip(data["class_ids"], row["reference_class_counts"], strict=True) if n}
                    != {r["expected_class_id"] for r in source["targets"]}):
                raise ValueError("Held-out score image, subject, model or reference class differs.")
            seen.add(row["project_entry_id"])
    if seen != set(expected):
        raise ValueError("Held-out score entries do not cover the complete evaluation request.")
    return datasets


def precision_recall_curves(value, *, subject_id=None):
    """Return all tied thresholds; None is the no-positive-prediction endpoint."""
    data = validate_pixel_score_evidence(value)
    entries = [r for r in data["entries"] if subject_id is None or r["subject_id"] == subject_id]
    if not entries:
        raise ValueError("This subject has no retained held-out pixel scores.")
    curves = []
    for index, class_id in enumerate(data["class_ids"]):
        bins = {}
        for entry in entries:
            for score, pos, neg in entry["histograms"][index]:
                counts = bins.setdefault(score, [0, 0])
                counts[0] += pos; counts[1] += neg
        positive = sum(p for p, n in bins.values())
        negative = sum(n for p, n in bins.values())
        curve = {"class_id": class_id, "positive_count": positive, "negative_count": negative,
                 "prevalence": positive / (positive + negative), "average_precision": None,
                 "status": "available" if positive else "no_positive_references", "points": []}
        if positive:
            points = [{"threshold": None, "precision": 1.0, "recall": 0.0,
                       "true_positive": 0, "false_positive": 0}]
            tp, fp, area = 0, 0, 0.0
            for score, (pos, neg) in sorted(bins.items(), reverse=True):
                tp += pos; fp += neg
                precision, recall = tp / (tp + fp), tp / positive
                area += (pos / positive) * precision
                points.append({"threshold": score, "precision": precision, "recall": recall,
                               "true_positive": tp, "false_positive": fp})
            curve["average_precision"] = area
            curve["points"] = points
        curves.append(curve)
    return curves
