"""Assign complete subjects to declared development, validation or test roles reproducibly.

The helpers use the supplied participant inventory, requested counts and seed to construct
explicit entry assignments. Every image from one subject receives the same role. The
returned assignments enter the validated training plan before reference evidence is
collected or a classifier is fitted."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
import hashlib
import json
from collections.abc import Mapping
from ._shared import BatchTrainingError

ALGORITHM = "subject-hash-ranked-holdout/v1"


# ==============================================================================
#                                  Split policy
# ==============================================================================


def assign_subject_roles(
    entry_subjects: Mapping[str, str],
    *,
    validation_subjects: int,
    test_subjects: int,
    seed: int,
    evaluation_intent: str,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(entry_subjects, Mapping) or not entry_subjects:
        raise BatchTrainingError("The training plan requires explicit image and subject identities")
    if any(
        type(k) is not str or not k.strip() or type(v) is not str or not v.strip()
        for k, v in entry_subjects.items()
    ):
        raise BatchTrainingError("Each included image requires its subject identity")
    for label, value in (
        ("validation subject count", validation_subjects),
        ("test subject count", test_subjects),
        ("split seed", seed),
    ):
        if type(value) is not int or value < 0 or value >= 2**31:
            raise BatchTrainingError(f"{label} must be a nonnegative integer")
    subjects = sorted(set(entry_subjects.values()))
    if validation_subjects + test_subjects >= len(subjects):
        raise BatchTrainingError(
            "Reserve at least one complete subject for training; reduce the validation/test subject counts"
        )
    if evaluation_intent == "training_only":
        if validation_subjects or test_subjects:
            raise BatchTrainingError(
                "Training-only mode does not include validation or test subjects"
            )
    elif evaluation_intent == "exploratory":
        if validation_subjects < 1:
            raise BatchTrainingError(
                "Exploratory evaluation requires at least one separate validation subject"
            )
    elif evaluation_intent == "independent_performance":
        if test_subjects < 1:
            raise BatchTrainingError(
                "Independent evaluation requires at least one separate test subject"
            )
    else:
        raise BatchTrainingError("Select a supported evaluation mode")

    def rank(subject):
        value = json.dumps(
            [ALGORITHM, seed, subject], ensure_ascii=False, separators=(",", ":")
        ).encode()
        return hashlib.sha256(value).digest(), subject

    ranked = sorted(subjects, key=rank)
    roles = {
        subject: (
            "test"
            if i < test_subjects
            else "validation" if i < test_subjects + validation_subjects else "training"
        )
        for i, subject in enumerate(ranked)
    }
    return tuple((entry, roles[subject]) for entry, subject in sorted(entry_subjects.items()))
