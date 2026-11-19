"""

Saves an automatic alignment request on the cluster and submits its Slurm job.

We send the selected image-pair review and fitting parameters together.
Before queueing the calculation, the cluster checks that the request still
describes the current analysis step and writes it under a unique submission
ID. The job later reads that saved request and calls the same alignment
operation used for local processing.

The submission sends parameters and file references through SSH. The image
readers and fitting programs access the source images on the cluster during
the allocated job. Environment activation and resource allocation are added
by the shared Slurm submission functions.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

import json, re, uuid
from ..batch_cli import _response_text
from .cluster_annotations import _folder
from .cluster_annotations import _new
from .cluster_annotations import _read
from .cluster_annotations import _sync

# ==============================================================================
#                               Cluster alignment
# ==============================================================================


def stage_alignment(service, batch_id, text):
    """

    Saves the requested automatic alignment after checking its analysis step.

    We decode the submitted JSON with repeated-field and nonfinite-number checks,
    then read the review request through the current response decoder. While
    holding the analysis connection, we compare its expected state with the
    cluster's current state. A matching request receives a new submission ID
    and is written to the cluster review folder before that ID is returned.

    Args:
        service: Cluster-side analysis service containing the current analysis
            state and review folder.
        batch_id: Analysis ID to which the selected image pair belongs.
        text: JSON text containing request and options, limited to 1 MiB.

    Returns:
        Dictionary containing the submission_id used to start the fitting job.

    Raises:
        ValueError: Text or field values are invalid, the analysis is unavailable,
            or the alignment request describes an earlier analysis state.
        OSError: Saving or synchronizing the submitted request fails.
    """

    if not isinstance(text, str) or len(text.encode()) > 1024 * 1024:
        raise ValueError("Alignment request exceeds its size limit.")

    def unique(pairs):
        """

        Reads one JSON object while requiring each field name to appear once.

        Args:
            pairs: Ordered key/value pairs supplied by the JSON decoder.

        Returns:
            Dictionary containing the submitted values.

        Raises:
            ValueError: A repeated key would replace an earlier submitted value.
        """

        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Repeated alignment request field")
            result[key] = value
        return result

    value = json.loads(
        text,
        object_pairs_hook=unique,
        parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("Nonfinite alignment setting")),
    )
    if (
        not isinstance(value, dict)
        or set(value) != {"request", "options"}
        or not isinstance(value["options"], dict)
    ):
        raise ValueError("Select the current alignment review and its settings.")
    request = _response_text(json.dumps(value["request"], allow_nan=False))
    with service._provider._use():
        service._require_batch(batch_id)
        if service.status(batch_id).state_head_sha256 != request.expected_state_head_sha256:
            raise ValueError(
                "The alignment review has changed. Refresh it before queuing alignment."
            )
        folder = _folder(service)
        identifier = uuid.uuid4().hex
        _new(folder / (identifier + ".alignment.json"), value)
        _sync(folder)
        return {"submission_id": identifier}


def run_alignment(service, batch_id, identifier):
    """

    Reads a saved submission and executes it through the analysis's alignment operation.

    The Slurm command supplies the submission ID rather than repeating the
    complete entered settings on its command line. We retrieve the saved
    request, decode its current response fields and pass its options to the
    analysis service, which checks the image pair and prepares the transform.

    Args:
        service: Analysis service opened by the cluster job.
        batch_id: Analysis ID selected for the job.
        identifier: Thirty-two-character hexadecimal submission ID returned
            when the request was staged.

    Returns:
        Result of service.align_paired_image(), including the proposed alignment
        and its next review information.

    Raises:
        ValueError: The submission ID, saved request or current analysis inputs
            fail their checks.
    """

    if not isinstance(identifier, str) or not re.fullmatch("[0-9a-f]{32}", identifier):
        raise ValueError("Choose the submitted alignment job.")
    value = _read(_folder(service) / (identifier + ".alignment.json"))
    request = _response_text(json.dumps(value["request"], allow_nan=False))
    return service.align_paired_image(batch_id, request, value["options"])


def submit(client, request, options):
    """

    Transfers the current alignment settings and submits the corresponding cluster job.

    We first call the cluster's stage-alignment command with the complete JSON
    request. The returned submission ID is checked before we construct the
    align-staged command and pass it to the shared Slurm submitter. This keeps
    the queued calculation tied to the parameter values that were actually saved.

    Args:
        client: Configured SSH/Slurm client for the cluster analysis.
        request: Current alignment review response selected in the desktop.
        options: Entered automatic affine and B-spline parameters with the
            cluster's Elastix/Transformix path.

    Returns:
        Submitted-job description returned by the shared Slurm submitter.

    Raises:
        ValueError: The request exceeds 1 MiB or the cluster does not return
            the saved submission ID.
    """

    from .cluster_review_pages import _plain
    from .cluster_annotation_jobs import _submit
    from .cluster_annotation_jobs import _command

    payload = json.dumps({"request": _plain(request), "options": options}, allow_nan=False)
    if len(payload.encode()) > 1024 * 1024:
        raise ValueError("Alignment request exceeds its size limit.")
    command = _command(client, "stage-alignment")
    staged = json.loads(client._call(client.prepared_command(command), input=payload))
    identifier = staged.get("submission_id") if isinstance(staged, dict) else None
    if not isinstance(identifier, str) or not re.fullmatch("[0-9a-f]{32}", identifier):
        raise ValueError("The cluster did not retain the alignment request.")
    return _submit(client, "alignment", _command(client, "align-staged", identifier))
