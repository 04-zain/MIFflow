"""

Translates the researcher's training procedure into two decisions: whether to
compare hyperparameters through cross-validation, and whether to reserve test subjects.

Training only and training with independent testing use one value per model
parameter. Procedures with validation compare the entered parameter combinations
within the development subjects, then refit the selected configuration using those
subjects. The same procedure drives the visible fields and the submitted settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

# ==============================================================================
#                           Training-procedure choices
# ==============================================================================

LABELS = {
    "training_only": "Training only",
    "training_validation": "Training and validation (cross-validation)",
    "training_test": "Training and independent testing",
    "training_validation_test": "Training, validation and independent testing",
}


# ==============================================================================
#                               Training procedure
# ==============================================================================


@dataclass(frozen=True, slots=True)
class TrainingProcedure:
    """

    Stores the fitting and evaluation choices associated with one training procedure.

    Attributes:
        name: Procedure identifier used by the classifier form.
        cross_validation: Whether candidate settings are compared across subject folds.
        independent_test: Whether a separate group of subjects is reserved for testing.
    """

    name: str
    cross_validation: bool
    independent_test: bool

    @property
    def evaluation_intent(self) -> str:
        """

        Translates test reservation into the evaluation value expected by the training service.

        Returns:
            "independent_performance" when test subjects are reserved; otherwise "training_only".
        """

        return "independent_performance" if self.independent_test else "training_only"


PROCEDURES = {
    "training_only": TrainingProcedure("training_only", False, False),
    "training_validation": TrainingProcedure("training_validation", True, False),
    "training_test": TrainingProcedure("training_test", False, True),
    "training_validation_test": TrainingProcedure("training_validation_test", True, True),
}


def selected_procedure(values: Mapping[str, object], index: int) -> TrainingProcedure:
    """

    Reads the training procedure chosen for one classifier.

    The form names each classifier's controls using its position, for example
    "training_0_procedure". This lookup gives the presenter and submission code the
    same fitting and test-reservation decisions.

    Args:
        values: Current form values indexed by control name.
        index: Classifier position in the form's ordered list.

    Returns:
        The TrainingProcedure corresponding to the selected option.

    Raises:
        ValueError: A procedure is missing or is outside the offered choices.
    """

    choice = values.get(f"training_{index}_procedure")
    if choice not in PROCEDURES:
        raise ValueError("Choose a training procedure before fitting the classifier.")
    return PROCEDURES[choice]
