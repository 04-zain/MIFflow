"""Plain desktop summaries and extended explanations for service errors.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import re

_MISSING_RUN_STATE = (
    "state head is missing from initialized execution",
    "state head is missing from retained execution",
    "active batch wave lacks execution state",
)
_MISSING_RUN_GUIDANCE = (
    "The saved progress record is unavailable, so this analysis cannot safely resume. "
    "Keep the analysis folder unchanged. If you have a complete copy, open it; "
    "otherwise create a new analysis from the original images."
)
_UNMIXING_ERROR_COPY = {
    "working-array budget cannot hold one numerical pixel and its shard/codec buffers": (
        "The memory allowance for unmixing is too small for one pixel's numerical "
        "calculation together with the image-writing and compression buffers. "
        "This calculation requires a larger working-array allowance."
    ),
    "native numerical thread count is unknown or invalid": (
        "The app cannot determine how many CPU threads the libraries used for unmixing will use. "
        "Update or reinstall the supported app and dependencies before running unmixing."
    ),
}
_CLASSIFIER_SAVED_RESULT_GUIDANCE = (
    "Keep the analysis folder unchanged. Use a complete backup that can be "
    "restored to its original location, or create a new analysis from the original images."
)
_CLASSIFIER_COMPLETION_ERROR_COPY = {
    "native fit completion proof is invalid": (
        "The record confirming that classifier training finished could not be verified. "
        + _CLASSIFIER_SAVED_RESULT_GUIDANCE
    ),
    "checkpointed native fit completed archive is missing": (
        "The record confirming that classifier training finished is missing. "
        + _CLASSIFIER_SAVED_RESULT_GUIDANCE
    ),
    "checkpointed native fit completed archive differs": (
        "The saved completion record does not match this training run. "
        + _CLASSIFIER_SAVED_RESULT_GUIDANCE
    ),
    "checkpointed native fit completed archive is unavailable or invalid": (
        "The record confirming that classifier training finished cannot be read or verified. "
        + _CLASSIFIER_SAVED_RESULT_GUIDANCE
    ),
    "native fit archive immutable copies differ": (
        "The saved training-completion records disagree. " + _CLASSIFIER_SAVED_RESULT_GUIDANCE
    ),
    "native fit original active record reference differs": (
        "The saved training records do not refer to the same run. "
        + _CLASSIFIER_SAVED_RESULT_GUIDANCE
    ),
    "native fit completion authority is stale": (
        "The displayed classifier training result is no longer current. "
        "Reopen the analysis and review the current result."
    ),
    "native fit completion has an unresolved current mutation": (
        "Classifier training completion cannot be confirmed because a change "
        "remains unfinished. Keep the analysis open and check its current status."
    ),
    "project control requires completed native fit proof": (
        "Classifier training completion has not been confirmed. "
        "Keep the analysis open and check its current status."
    ),
}
_PROJECT_EDIT_GUIDANCE = (
    "The application has not confirmed how the previous QuPath operation finished. "
    "Wait for any running task to finish, or close its idle review window, then "
    "refresh Run to check the recorded operation. Keep the analysis "
    "folder intact while resolving the reported error."
)
_PROJECT_EDIT_REASONS = (
    "an unresolved project mutation requires restricted recovery",
    "project control authority differs from state",
)

_RETRY_STALE_GUIDANCE = (
    "The selected step is no longer current. Refresh and choose the step again."
)
_RETRY_CHANGED_GUIDANCE = (
    "The saved image or annotations no longer match the failed step. "
    "This step cannot be retried with the changed data. Keep the analysis folder "
    "unchanged and review the current data before starting a new analysis."
)
_RETRY_ERROR_COPY = {
    "retry selection is not the current failed node": _RETRY_STALE_GUIDANCE,
    "project-stage retry saved image state changed": _RETRY_CHANGED_GUIDANCE,
    "project process registry contains an undrained launch": (
        "QuPath has not been confirmed to have stopped. Check whether it is still "
        "running and refresh after it finishes. Keep the analysis folder unchanged "
        "if retry remains unavailable."
    ),
    "project process registry drain proof is incomplete": (
        "The record confirming that QuPath stopped is incomplete. This step cannot "
        "be retried safely. Keep the analysis folder unchanged."
    ),
    "attempt authorization must use the current v2 schema": (
        "This analysis uses a saved-progress format that this version cannot open. "
        "Keep its existing results and create a new analysis from the original images."
    ),
}
_ACTION_ERROR_COPY = {
    "Open an analysis before viewing its saved images.": (
        "No analysis is open. Select Open analysis to choose the analysis whose saved images you want to view."
    ),
    "Refresh the analysis before continuing its saved work.": (
        "The saved progress has changed. Select Refresh in Run to load its current state."
    ),
    "Select an absolute analysis folder": (
        "The analysis folder needs a full location. Choose a folder starting from the drive or filesystem root."
    ),
    "a workspace is already open": (
        "An analysis is already open. Close the current analysis before opening another."
    ),
    "a workspace is already open or opening": (
        "An analysis is already open or loading. Let it finish loading, then close it before opening another."
    ),
    "another run or mutation job is active": (
        "Another analysis action is still in progress. Wait for it to finish, then try again."
    ),
    "a control job is already active": (
        "A pause or cancel request is already in progress. Wait for that request to finish."
    ),
    "control requires the exact active run job": (
        "The processing step has already stopped or changed. Refresh Run to see its current status."
    ),
    "control requires an exact active run job ID": (
        "The app could not identify the processing step to pause or cancel. "
        "Refresh Run and try again. If this repeats, copy Details to report the application error."
    ),
    "mutation guard is already bound": (
        "The app could not initialize its analysis controls. Restart the app and reopen "
        "the analysis. If this repeats, copy Details to report the application error."
    ),
    "reference interaction owner context is stale": (
        "The app could not match this form to the saved spectral review. Return to Workflow "
        "and reopen Spectral unmixing. If your selections have not changed and this repeats, "
        "restart the updated app and copy Details to report the application error."
    ),
    "Stage-1 interaction owner context is stale": (
        "This form does not match the saved channel review. Return to Workflow and reopen "
        "Channels & markers. If this repeats without changing your selections, copy Details "
        "to report the application error."
    ),
    "interaction response head authority changed": (
        "The saved analysis changed while this form was open. Return to Workflow, "
        "open the current review, and check your choices before saving them."
    ),
    "reference interaction has unsupported source input": (
        "The app supplied inputs that this spectral review cannot use. Keep the analysis "
        "folder unchanged and copy Details to report the application error."
    ),
    "application interaction owners do not match the registered stages": (
        "The installed review forms do not match the app's processing stages. Restart "
        "the updated app and reopen the analysis. If this repeats, copy Details to report it."
    ),
}


# ==============================================================================
#                                   Error copy
# ==============================================================================


def operation_error_summary(detail: str, *, continuation_available: bool = False) -> str | None:
    """Give a safe next step only when the diagnostic identifies the condition."""

    for prefix in (
        "Analysis could not start or continue.\n",
        "bounded scheduler failed: ",
        "Revised options could not be applied.\n",
        "Progress check also failed:\n",
    ):
        if detail.startswith(prefix):
            cause = operation_error_summary(
                detail.removeprefix(prefix).partition("\n\nProgress check also failed:\n")[0],
                continuation_available=continuation_available,
            )
            if cause is not None:
                return cause
            break
    if detail == (
        "The saved analysis and this application's processing steps do not match. "
        "Create a new analysis with the current application build."
    ):
        return detail
    if detail in _ACTION_ERROR_COPY:
        return _ACTION_ERROR_COPY[detail]
    if detail.startswith("The saved QuPath run has no process-completion record."):
        return (
            "The saved QuPath run has no process-completion record. Its exit status "
            "and completed process cleanup are unknown. For an interrupted first segmentation, "
            "Restore accepted regions can check that the prior local process is absent, "
            "back up the interrupted state and restore verified tissue and primary regions. "
            "The old result stays unaccepted. Other cases require the retained records to be resolved."
        )
    if detail.startswith("Saved training references are missing for "):
        lines = detail.splitlines()
        if len(lines) == 3:
            return "\n".join(
                line if len(line) <= limit else line[:limit] + "… See Details for the remaining classes."
                for line, limit in zip(lines, (600, 900, 1800))
            )
        return detail[:3300]
    access_error = re.search(
        r"(?:\[Errno (?:13|30)\] (?:Permission denied|Read-only file system): "
        r"|java\.nio\.file\.AccessDeniedException: )([^\n]+)", detail
    )
    if access_error:
        if "[Errno 30] Read-only file system:" in detail:
            return (
                "The app cannot save to a read-only drive. Restore write access to the drive "
                "listed in Details before saving to this location."
            )
        return (
            "The app does not have permission to access a required file or folder. "
            "Restore access to the location listed in Details."
        )
    missing_file = re.search(
        r"(?:\[Errno 2\] No such file or directory: "
        r"|java\.nio\.file\.NoSuchFileException: )([^\n]+)", detail
    )
    if missing_file:
        return (
            "A required file could not be found. Open Details for its location. "
            "Reconnect the drive or restore the file to that location if it was moved. "
            "If the file was never created, copy Details to report the missing output."
        )
    if any(text in detail for text in (
        "recorded project mutation process is still live",
        "The previous QuPath image update has not been confirmed as finished.",
    )):
        return (
            "The application has not confirmed that the previous QuPath image update finished. "
            "If QuPath is still processing, wait for it to finish. Then refresh Run to check "
            "the recorded operation. If no QuPath task is running and this message "
            "appears again, use Copy details in Run to report the recovery problem."
        )
    if detail.startswith("manifest draft is invalid: "):
        fields = {
            "subject_id": "Subject ID",
            "sample_id": "Sample name", "validation_domain_id": "Processing group",
        }
        issues = detail.removeprefix("manifest draft is invalid: ")
        issues = re.sub(
            r"\b(subject_id|sample_id|validation_domain_id)\b",
            lambda match: fields[match.group()], issues,
        )
        return "Review the information in Samples, then save the analysis again.\n\n" + issues
    if any(reason in detail for reason in _PROJECT_EDIT_REASONS):
        return _PROJECT_EDIT_GUIDANCE
    # Match a Python exception line or NumPy's actual allocation diagnostic,
    # rather than a mention of memory or an echoed JVM launch option.
    if re.search(
        r"(?m)^(?:MemoryError(?::[^\r\n]*)?"
        r"|(?:numpy\._?core\._exceptions\._ArrayMemoryError: )?"
        r"Unable to allocate \d+(?:\.\d*)? (?:bytes|[KMGTPE]iB) "
        r"for an array with shape \([\d, ]+\) and data type [^\r\n]+)$",
        detail,
    ):
        return (
            "The application calculation ran out of working memory. Close unused applications or image windows, "
            "then open the stopped step to review its recovery controls. "
            "Details contains the original memory error."
        )
    if any(
        reason in detail
        for reason in (
            "Java heap space",
            "GC overhead limit exceeded",
            "java.lang.OutOfMemoryError",
        )
    ):
        return (
            "QuPath ran out of working memory. Close unused applications or image windows, "
            "then open the stopped step to review its recovery controls. "
            "Details contains the original memory error."
        )
    reported = next((line.partition("QuPath reported: ")[2] for line in detail.splitlines()
                     if line.startswith("QuPath reported: ")), "")
    if reported:
        reported = re.sub(r"^(?:Caused by:\s*)?(?:[\w$]+\.)*[\w$]*(?:Exception|Error):\s*", "", reported)
        reported = re.sub(r" in [\w./-]+\.groovy at line number \d+.*$", "", reported)
        return "QuPath could not complete this step.\n\nReported cause: " + reported + "\n\nDetails contains the full error report."
    if detail.startswith("Check the spectral references:"):
        return detail
    if detail in _UNMIXING_ERROR_COPY:
        return _UNMIXING_ERROR_COPY[detail]
    if detail in _CLASSIFIER_COMPLETION_ERROR_COPY:
        return _CLASSIFIER_COMPLETION_ERROR_COPY[detail]
    if detail in _RETRY_ERROR_COPY:
        return _RETRY_ERROR_COPY[detail]
    if detail.startswith("Analysis could not start or continue."):
        return (
            "We could not determine why this action stopped. "
            "Open Details to inspect the original error, or copy the details to report the problem."
        )
    if (
        "application interaction inventory must contain exactly " in detail
        or "application interaction owners do not match the registered stages" in detail
    ):
        return (
            "The app could not complete this action. Keep the analysis folder unchanged "
            "and reopen the analysis after the app is updated."
        )
    if any(reason in detail for reason in _MISSING_RUN_STATE):
        return _MISSING_RUN_GUIDANCE
    if "pause requires an active batch wave" in detail:
        return "There is no active analysis to pause. Check Run for its current status."
    if "resume requires retained state" in detail:
        return _MISSING_RUN_GUIDANCE
    if detail.startswith("File already exists"):
        return "A file already exists at this location. Choose a different location."
    return None


def diagnostic_details(detail: str, explanation: str) -> str:
    """Keep the exact original diagnostic after its readable summary."""
    return explanation if detail == explanation else f"{explanation}\n\n{detail}"


def field_validation_summary(
    detail: str,
    label: str,
    *,
    field_name: str | None = None,
) -> str:
    """Preserve known field constraints and keep unknown validator text in Details."""

    if detail == "image_index and series_index must be supplied together":
        return "Choose both image and series numbers, or leave both blank."
    if field_name is not None and (
        f"duplicate {field_name}" in detail.lower()
        or (field_name == "slide_id" and detail == "Duplicate slide")
    ):
        return f"{label} is used more than once. Choose a different {label}."
    if field_name is not None and detail.startswith(f"{field_name} must be at most "):
        bound = detail.removeprefix(f"{field_name} must be at most ").rstrip(".")
        if bound.replace(".", "", 1).isdigit():
            return f"{label} must be at most {bound}. Enter a value no greater than {bound}."
    if field_name is not None and detail == f"{field_name} is required":
        return f"{label} is missing. Enter a value, then try again."
    if field_name is not None and detail == f"{field_name} must be an integer":
        return f"Enter a whole number for {label}, then try again."
    if field_name is not None and detail == f"{field_name} must be a finite number":
        return f"Enter a number for {label}, then try again."
    if field_name is not None and detail in {
        f"{field_name} must be one of the offered choices",
        f"{field_name} requires a choice",
    }:
        return f"Choose one of the available values for {label}, then try again."
    if field_name is not None and detail == (
        f"{field_name} must contain valid JSON with finite numbers and no duplicate keys"
    ):
        return (
            f"Enter a valid value for {label}. Remove duplicate keys and non-finite numbers, "
            "then try again."
        )
    return f"Check {label}, then try again. Open Details for the exact validation rule."


__all__ = ["diagnostic_details", "field_validation_summary", "operation_error_summary"]
