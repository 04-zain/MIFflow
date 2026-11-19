"""Schedule native annotation work; only small review records cross the SSH link.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import PurePosixPath
import hashlib, json, re, shlex, uuid

# ==============================================================================
#                            Cluster annotation jobs
# ==============================================================================


def _command(client, operation, *values):
    p = client.profile
    args = [p.batch_executable]
    for key, value in sorted(p.runtime_options.items()):
        args.extend(["--" + key, value])
    return [*args, operation, p.output_root, p.batch_id, *values]


STAGE_OPERATIONS = frozenset({"status", "runtime-capabilities", "stage-options", "preview-stage-revision", "revise-stage",
                              "list-retries", "retry", "continue", "review-figures"})


class ClusterJobFailed(ValueError):
    """A submitted job has ended; its saved failure can be corrected or retried."""


def submission_name(client, token=""):
    """Keep a stable analysis prefix and an exact identifier for one submission."""
    if token and not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("The saved cluster submission identifier is invalid.")
    identity = json.dumps([client.profile.output_root, client.profile.batch_id], separators=(",", ":"))
    return "mif-" + hashlib.sha256(identity.encode()).hexdigest()[:16] + "-" + token


def submit_script(client, operation, script, *, token=None):
    """Retain the scheduler receipt remotely before returning across the SSH link.

    The application saves its token before calling this helper. A lost reply
    can therefore be reconciled using the receipt or the exact scheduler name.
    This helper never retries sbatch after a transport error.
    """
    token = token or uuid.uuid4().hex
    name = submission_name(client, token)
    receipt = str(PurePosixPath(client.profile.working_directory) / (".mif-submission-" + token + ".json"))
    command = shlex.join(["sbatch", "--parsable", *client._scheduler_options(), "--job-name=" + name])
    wrapper = (
        "set -e\numask 077\n"
        'if test -f "$1"; then cat -- "$1"; exit 0; fi\n'
        + "reply=$(" + command + ")\n"
        + 'job=${reply%%;*}\n[[ "$job" =~ ^[1-9][0-9]{0,17}$ ]] || exit 3\n'
        + "printf '{\"job_id\":\"%s\",\"operation\":\"%s\"}\\n' \"$job\" \"$2\" > \"$1.part\"\n"
        + 'mv -- "$1.part" "$1"\ncat -- "$1"\n'
    )
    response = client._call(client.prepared_command(
        ["/bin/bash", "--noprofile", "--norc", "-c", wrapper, "mif-submit", receipt, operation]), input=script)
    value = json.loads(response)
    if not isinstance(value, dict) or set(value) != {"job_id", "operation"} or value["operation"] != operation:
        raise ValueError("The cluster submission receipt differs from the requested operation.")
    client._job(value["job_id"])
    return value["job_id"]


def _submit(client, operation, command, *, input_text=None, token=None):
    """Queue an existing CLI operation with its result and optional bounded input.

    The same allocation carries annotation work and stage-review controls.
    Only sbatch runs through the SSH connection; image readers, prerequisite
    probes and analysis commands run inside the requested allocation.
    """
    if input_text is not None and (not isinstance(input_text, str)
                                  or len(input_text.encode("utf-8")) > 1024 * 1024):
        raise ValueError("Stage options exceed 1 MiB.")
    p = client.profile
    token = token or uuid.uuid4().hex
    submission_name(client, token)
    result = PurePosixPath(p.working_directory) / (".mif-annotations-" + token + ".json")
    partial = str(result) + ".part"
    error = str(result) + ".stderr"
    script = (
        "#!/bin/bash\n"
        + "".join("#SBATCH " + shlex.quote(option) + "\n" for option in client._scheduler_options())
        + client.environment_lines()
        + "umask 077\n"
        + "unset MIF_SELECTIVE_COMPUTE_PROFILE\n"
        + "export MIF_COMPUTE_DEVICE=" + shlex.quote(p.device) + "\n"
        + "if "
        + ("printf '%s' " + shlex.quote(input_text) + " | " if input_text is not None else "")
        + shlex.join(command)
        + " > "
        + shlex.quote(partial)
        + " 2> "
        + shlex.quote(error)
        + "; then\n"
        + "  mv -- "
        + shlex.quote(partial)
        + " "
        + shlex.quote(str(result))
        + "\nelse\n"
        + "  status=$?\n"
        + ("  if test \"$status\" -eq 1 && test -s " + shlex.quote(partial)
           + "; then mv -- " + shlex.quote(partial) + " " + shlex.quote(str(result))
           + '; exit "$status"; fi\n' if operation in {"retry", "continue"} else "")
        + "  printf '%s\\n' "
        + shlex.quote(json.dumps({"status": "failed", "error_file": error}))
        + " > "
        + shlex.quote(str(result))
        + '\n  exit "$status"\nfi\n'
    )
    job = submit_script(client, operation, script, token=token)
    return {"job_id": job, "operation": operation, "result_file": str(result)}


def submit_export(client, request_id, expected_head, entry_id=None, *, preview_selection=None, reference_role=None):
    if not isinstance(request_id, str) or not re.fullmatch("[A-Za-z0-9_-]{1,160}", request_id):
        raise ValueError("Choose a current annotation request.")
    if not isinstance(expected_head, str) or not re.fullmatch("[0-9a-f]{64}", expected_head):
        raise ValueError("Refresh the current cluster review.")
    if entry_id is not None and (
        not isinstance(entry_id, str) or not re.fullmatch("[A-Za-z0-9_-]{1,160}", entry_id)
    ):
        raise ValueError("Select an image from the current review.")
    extra = [] if entry_id is None else ["--entry-id", entry_id]
    if reference_role is not None:
        extra.extend(["--reference-role", json.dumps(reference_role, sort_keys=True, allow_nan=False)])
    if preview_selection is not None:
        payload = json.dumps(preview_selection, sort_keys=True, allow_nan=False)
        if len(payload.encode()) > 1024 * 1024:
            raise ValueError("The annotation-preview settings exceed the permitted size.")
        reply = json.loads(
            client._call(
                client.prepared_command(_command(client, "stage-annotation-preview")), input=payload
            )
        )
        identifier = reply.get("submission_id")
        if not re.fullmatch("[0-9a-f]{32}", str(identifier)):
            raise ValueError("The cluster did not retain the preview settings.")
        extra.extend(["--preview-id", identifier])
    return _submit(
        client,
        "annotations-export",
        _command(client, "annotations-export", request_id, expected_head, *extra),
    )


def submit_import(client, ticket_id, objects):
    if not isinstance(ticket_id, str) or not re.fullmatch("[0-9a-f]{32}", ticket_id):
        raise ValueError("Open a current annotation review before returning edits.")
    payload = json.dumps(
        {"ticket_id": ticket_id, "objects": objects}, sort_keys=True, allow_nan=False
    )
    if len(payload.encode()) > 32 * 1024 * 1024:
        raise ValueError("This annotation submission exceeds 32 MiB.")
    reply = json.loads(
        client._call(client.prepared_command(_command(client, "stage-annotations")), input=payload)
    )
    submission = reply.get("submission_id") if isinstance(reply, dict) else None
    if not isinstance(submission, str) or not re.fullmatch("[0-9a-f]{32}", submission):
        raise ValueError("The cluster did not retain the annotation submission.")
    return _submit(client, "annotations-import", _command(client, "annotations-import", submission))


def result(client, receipt):
    if not isinstance(receipt, dict) or set(receipt) != {"job_id", "operation", "result_file"}:
        raise ValueError("Select the submitted annotation job.")
    client._job(receipt["job_id"])
    path = PurePosixPath(receipt["result_file"])
    if (
        path.parent != PurePosixPath(client.profile.working_directory)
        or not re.fullmatch(r"\.mif-annotations-[0-9a-f]{32}\.json", path.name)
        or receipt["operation"]
        not in STAGE_OPERATIONS | {
            "annotations-export",
            "annotations-import",
            "alignment",
            "landmark-export",
            "landmark-import",
        }
    ):
        raise ValueError("The result does not belong to this analysis job folder.")
    command = [
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-c",
        'if test -f "$1"; then size=$(wc -c < "$1"); '
        'test "$size" -le 33554432 || exit 3; cat -- "$1"; '
        "else printf '%s\\n' '{\"status\":\"pending\"}'; fi",
        "annotation-result",
        str(path),
    ]
    text = client._call(client.prepared_command(command), max_output_bytes=32 * 1024 * 1024)

    def unique(pairs):
        values = {}
        for key, value in pairs:
            if key in values:
                raise ValueError("Repeated annotation result field")
            values[key] = value
        return values

    reply = json.loads(
        text,
        object_pairs_hook=unique,
        parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("Nonfinite annotation result")),
    )
    if not isinstance(reply, dict):
        raise ValueError("The annotation job returned an invalid result.")
    if reply.get("status") == "pending":
        observation = client.status(receipt["job_id"])["scheduler"]
        terminal = {
            "FAILED",
            "TIMEOUT",
            "CANCELLED",
            "OUT_OF_MEMORY",
            "NODE_FAIL",
            "PREEMPTED",
            "BOOT_FAIL",
            "DEADLINE",
            "REVOKED",
            "COMPLETED",
        }
        for line in observation.splitlines():
            fields = line.split("|")
            if len(fields) < 2 or fields[0].strip() != receipt["job_id"]:
                continue
            state = fields[1].strip().split()[0].rstrip("+")
            if state in terminal:
                raise ClusterJobFailed(
                    "Cluster job "
                    + receipt["job_id"]
                    + " ended with status "
                    + state
                    + " before a review result was available. Open its job log to check the environment and requested resources, then retry this review. Saved image results are retained."
                )
    if reply.get("status") == "failed":
        error_path = str(path) + ".stderr"
        details = client._call(client.prepared_command(["tail", "-c", "4000", "--", error_path]))
        raise ClusterJobFailed("The cluster operation failed. " + details[-4000:])
    return reply
