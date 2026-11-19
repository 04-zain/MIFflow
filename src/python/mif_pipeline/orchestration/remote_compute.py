"""Prepare and control declared pipeline jobs over SSH and Slurm.

The client builds commands from the selected cluster program, environment and resource
settings. Preparation and review requests use bounded structured data; job receipts retain
the scheduler response and prevent duplicate submission after an uncertain reply. The
cluster owns its analysis state and project writes."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from dataclasses import dataclass, asdict, field
from pathlib import PurePosixPath, Path
import argparse, json, re, shlex, subprocess, sys

# ==============================================================================
#                                 Remote compute
# ==============================================================================


@dataclass(frozen=True, slots=True)
class RemoteProfile:
    host: str
    batch_executable: str
    manifest: str
    output_root: str
    batch_id: str
    working_directory: str
    cpus: int = 4
    memory_gib: int = 16
    hours: int = 4
    gpus: int = 0
    partition: str = ""
    account: str = ""
    runtime_options: dict = field(default_factory=dict)
    device: str = "auto"
    # Only retained for pending analyses authored before stage-specific precision.
    precision: str | None = None
    environment_script: str = ""
    modules: tuple[str, ...] = ()
    kronos_package_directory: str | None = None
    kronos_marker_text_model: dict | None = None

    def __post_init__(self):
        if self.kronos_marker_text_model is not None:
            from mif_pipeline.compartment_analysis.kronos_marker_preparation import KronosMarkerTextModel
            helper = KronosMarkerTextModel.from_mapping(self.kronos_marker_text_model)
            if not PurePosixPath(helper.directory).is_absolute():
                raise ValueError("Select the marker-description model from the mounted cluster folder")
        model = self.kronos_package_directory
        if model is not None and (not isinstance(model, str)
                or not PurePosixPath(model).is_absolute()
                or ".." in PurePosixPath(model).parts
                or any(ord(c) < 32 for c in model)):
            raise ValueError("Select the KRONOS model from the mounted cluster folder in Software setup.")
        if not isinstance(self.environment_script, str) or (
            self.environment_script
            and (
                not PurePosixPath(self.environment_script).is_absolute()
                or any(ord(c) < 32 for c in self.environment_script)
            )
        ):
            raise ValueError("Select the absolute cluster environment script path.")
        if (
            not isinstance(self.modules, tuple)
            or len(self.modules) > 32
            or any(
                not isinstance(item, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+/:-]{0,255}", item)
                for item in self.modules
            )
        ):
            raise ValueError("Enter module names and versions separated by spaces.")
        from mif_pipeline._compute_preferences import validate_compute_device, validate_compute_preferences

        validate_compute_device(self.device)
        if self.precision is not None:
            validate_compute_preferences(self.device, self.precision)
        if self.device == "mps":
            raise ValueError("Cluster jobs support CPU or NVIDIA GPU processing.")
        if self.device == "cuda" and self.gpus == 0:
            raise ValueError("Request at least one GPU for NVIDIA processing.")
        if not isinstance(self.host, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,252}", self.host
        ):
            raise ValueError("Enter an SSH host alias or user@host without command options")
        for name in ("batch_executable", "manifest", "output_root", "working_directory"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not PurePosixPath(value).is_absolute()
                or any(c in value for c in ("\0", "\n", "\r"))
            ):
                raise ValueError("Remote paths must be absolute paths on the cluster")
        if not isinstance(self.batch_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.batch_id
        ):
            raise ValueError("Enter the batch name recorded in the remote manifest")
        for name, lo, hi in (
            ("cpus", 1, 256),
            ("memory_gib", 1, 4096),
            ("hours", 1, 168),
            ("gpus", 0, 16),
        ):
            value = getattr(self, name)
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"{name} is outside the supported range")
        for name in ("account", "partition"):
            value = getattr(self, name)
            if not isinstance(value, str) or (
                value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value)
            ):
                raise ValueError("Scheduler account and partition must be simple names")
        allowed = {
            "qupath",
            "qupath-profile",
            "java-home",
            "bioformats-root",
            "instanseg-jar",
            "djl-jar",
            "pytorch-engine-jar",
            "cpu-native-root",
            "qupath-cache-root",
        }
        if not isinstance(self.runtime_options, dict) or set(self.runtime_options) - allowed:
            raise ValueError("Unknown remote runtime option")
        for value in self.runtime_options.values():
            if (
                not isinstance(value, str)
                or not PurePosixPath(value).is_absolute()
                or any(c in value for c in ("\0", "\n", "\r"))
            ):
                raise ValueError("Runtime program paths must be absolute remote paths")

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise ValueError("Remote profile contains unsupported fields")
        value = dict(value)
        if isinstance(value.get("modules"), list):
            value["modules"] = tuple(value["modules"])
        try:
            return cls(**value)
        except TypeError as e:
            raise ValueError(
                "Complete the remote host, program, manifest and workspace fields"
            ) from e

    def to_mapping(self):
        value = asdict(self)
        if self.precision is None:
            value.pop("precision")
        return value


class RemoteComputeClient:
    def __init__(self, profile, *, runner=subprocess.run, ssh_executable="ssh"):
        if type(profile) is not RemoteProfile:
            raise TypeError("Expected an explicit remote profile")
        self.profile = profile
        self.runner = runner
        self.ssh_executable = ssh_executable

    def _call(
        self, args, *, input=None, max_output_bytes=8 * 1024 * 1024, accepted_exit_codes=(0,)
    ):
        command = shlex.join(list(args))
        argv = [
            self.ssh_executable,
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=15",
            self.profile.host,
            command,
        ]
        try:
            result = self.runner(argv, input=input, text=True, capture_output=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ValueError(
                "The SSH connection could not be completed. Check the host, SSH keys and network."
            ) from e
        if result.returncode not in accepted_exit_codes:
            detail = (result.stderr or result.stdout)[-4000:]
            raise ValueError("SSH or scheduler operation failed: " + detail)
        if len(result.stdout.encode("utf-8")) > max_output_bytes:
            raise ValueError("Remote status exceeds the response limit")
        return result.stdout.strip()

    def check_environment(self):
        """Check prerequisites before any sample list or analysis file exists."""
        p = self.profile
        lines = [
            "if test -x "
            + shlex.quote(p.batch_executable)
            + "; then printf 'program|ready\\n'; else printf 'program|missing\\n'; fi",
            "if test -d "
            + shlex.quote(p.output_root)
            + " && test -w "
            + shlex.quote(p.output_root)
            + "; then printf 'results|ready\\n'; else printf 'results|missing\\n'; fi",
            "if command -v sbatch >/dev/null 2>&1; then printf 'scheduler|ready\\n'; else printf 'scheduler|missing\\n'; fi",
        ]
        raw = self._call(
            self.prepared_command(["/bin/bash", "--noprofile", "--norc", "-c", "\n".join(lines)])
        )
        checks = {}
        for line in raw.splitlines():
            parts = line.split("|")
            if (
                len(parts) != 2
                or parts[0] in checks
                or parts[0] not in {"program", "results", "scheduler"}
                or parts[1] not in {"ready", "missing"}
            ):
                raise ValueError("The cluster returned an invalid environment check.")
            checks[parts[0]] = parts[1]
        if set(checks) != {"program", "results", "scheduler"}:
            raise ValueError("The cluster environment check is incomplete.")
        return {"ready": all(v == "ready" for v in checks.values()), "checks": checks}

    def check_setup(self):
        """Read only the explicit paths and scheduler command; never search or transfer."""
        p = self.profile
        checks = (
            ("program", "-x", p.batch_executable),
            ("setup", "-r", p.manifest),
            ("results", "-d", p.output_root),
            ("job_folder", "-d", p.working_directory),
        )
        statements = []
        for key, flag, path in checks:
            statements.append(
                "if test "
                + flag
                + " "
                + shlex.quote(path)
                + "; then printf "
                + shlex.quote(key + "|ready\n")
                + "; else printf "
                + shlex.quote(key + "|missing\n")
                + "; fi"
            )
        statements.append(
            "if command -v sbatch >/dev/null 2>&1; then printf 'scheduler|ready\\n'; else printf 'scheduler|missing\\n'; fi"
        )
        result = self._call(
            self.prepared_command(
                ["/bin/bash", "--noprofile", "--norc", "-c", "\n".join(statements)]
            )
        )
        parsed = {}
        for line in result.splitlines():
            parts = line.split("|")
            if len(parts) != 2 or parts[0] in parsed or parts[1] not in {"ready", "missing"}:
                raise ValueError("The cluster returned an invalid setup check")
            parsed[parts[0]] = parts[1]
        if set(parsed) != {"program", "setup", "results", "job_folder", "scheduler"}:
            raise ValueError("The cluster setup check is incomplete")
        return {"ready": all(value == "ready" for value in parsed.values()), "checks": parsed}

    def batch_command(self, operation):
        if operation not in {
            "run",
            "resume",
            "status",
            "interactions",
            "review-page",
            "respond-stdin",
        }:
            raise ValueError("Unsupported remote batch operation")
        p = self.profile
        args = [p.batch_executable]
        for key, value in sorted(p.runtime_options.items()):
            args.extend(["--" + key, value])
        args.extend(
            [operation, p.manifest]
            if operation == "run"
            else [operation, p.output_root, p.batch_id]
        )
        return args

    def environment_lines(self):
        lines = ["set -eu"]
        if self.profile.environment_script:
            # Environment scripts are explicitly selected executable setup, not data.
            lines.append("source " + shlex.quote(self.profile.environment_script) + " >&2")
        if self.profile.modules:
            lines.append(
                'type module >/dev/null 2>&1 || { printf "%s\\n" "Module command is unavailable; select its environment initialization script." >&2; exit 2; }'
            )
            lines.append("module load " + shlex.join(self.profile.modules) + " >&2")
        if self.profile.kronos_package_directory:
            lines.append("export MIF_KRONOS_PACKAGE_DIRECTORY="
                         + shlex.quote(self.profile.kronos_package_directory))
        if self.profile.kronos_marker_text_model is not None:
            lines.append("export MIF_KRONOS_MARKER_TEXT_MODEL=" + shlex.quote(
                json.dumps(self.profile.kronos_marker_text_model, separators=(",", ":"))))
        return "\n".join(lines) + "\n"

    def prepared_command(self, args):
        # Only the Python entry point needs the declared pipeline environment.
        # Scheduler/file commands use the cluster's normal SSH environment;
        # every submitted job performs its own activation inside the allocation.
        setup = (
            self.environment_lines()
            if args and args[0] == self.profile.batch_executable
            else "set -eu\n"
        )
        return ["/bin/bash", "--noprofile", "--norc", "-c", setup + "exec " + shlex.join(args)]

    def _scheduler_options(self):
        p = self.profile
        args = [
            "--nodes=1",
            "--ntasks=1",
            "--dependency=singleton",
            "--job-name=mif-" + p.batch_id,
            "--cpus-per-task=" + str(p.cpus),
            "--mem=" + str(p.memory_gib) + "G",
            "--time=" + str(p.hours) + ":00:00",
            "--chdir=" + p.working_directory,
            "--output=" + str(PurePosixPath(p.working_directory) / "mif-%j.log"),
        ]
        if p.gpus:
            args.append("--gpus=" + str(p.gpus))
        if p.partition:
            args.append("--partition=" + p.partition)
        if p.account:
            args.append("--account=" + p.account)
        return args

    def slurm_script(self, operation="run"):
        """Render an inspectable job without connecting, transferring, or submitting."""
        if operation not in {"run", "resume"}:
            raise ValueError("Choose a new run or resume a saved analysis.")
        p = self.profile
        directives = "".join(
            "#SBATCH " + shlex.quote(option) + "\n" for option in self._scheduler_options()
        )
        return (
            "#!/bin/bash\n"
            + directives
            + self.environment_lines()
            + "unset MIF_SELECTIVE_COMPUTE_PROFILE\n"
            + "export MIF_COMPUTE_DEVICE="
            + p.device
            + "\n"
            + ("export MIF_COMPUTE_PRECISION=" + p.precision + "\n"
               if p.precision is not None else "unset MIF_COMPUTE_PRECISION\n")
            + "export MIF_SPECTRAL_PRECISION=device_selected\n"
            + "exec "
            + shlex.join(self.batch_command(operation))
            + "\n"
        )

    def submit(self, operation="run", *, token=None):
        """Submit the configured analysis command to the selected Slurm environment.

        The client builds the job from explicit program, environment and resource settings
        and retains the scheduler's submission identity. Its receipt is used to query or
        continue that job without creating an automatic duplicate after an uncertain
        transport reply."""

        p = self.profile
        script = self.slurm_script(operation)
        from .cluster.cluster_annotation_jobs import submit_script
        job = submit_script(self, operation, script, token=token)
        return {
            "job_id": job,
            "host": p.host,
            "operation": operation,
            "batch_id": p.batch_id,
            "log_pattern": str(PurePosixPath(p.working_directory) / "mif-%j.log"),
        }

    def reconcile_submission(self, pending):
        """Recover a receipt without submitting again or treating silence as absence.

        A successful empty queue and accounting lookup is reported to the UI
        for explicit review; accounting can lag behind scheduler acceptance.
        Connection errors and ambiguous matches remain unresolved.
        """
        from datetime import datetime, timezone
        from .cluster.cluster_annotation_jobs import submission_name, STAGE_OPERATIONS
        token, operation = pending["token"], pending["operation"]
        if operation not in STAGE_OPERATIONS | {"run", "resume"}:
            raise ValueError("The retained operation is invalid.")
        name = submission_name(self, token)
        path = str(PurePosixPath(self.profile.working_directory) / (".mif-submission-" + token + ".json"))
        raw = self._call(self.prepared_command(["/bin/bash", "--noprofile", "--norc", "-c",
            'if test -f "$1"; then test "$(wc -c < "$1")" -le 1024 || exit 3; cat -- "$1"; fi',
            "mif-receipt", path]))
        if raw:
            receipt = json.loads(raw)
            if not isinstance(receipt, dict) or set(receipt) != {"job_id", "operation"} or receipt["operation"] != operation:
                raise ValueError("The saved scheduler receipt differs from this operation.")
            job = self._job(receipt["job_id"])
        else:
            timestamp = pending["requested_at"]
            if type(timestamp) is not int or timestamp <= 0:
                raise ValueError("The saved submission time is invalid.")
            # A date avoids local/remote timezone ambiguity and includes the
            # preceding day at a UTC date boundary. Exact names select the job.
            since = datetime.fromtimestamp(max(0, timestamp - 86400), timezone.utc).strftime("%Y-%m-%d")
            queue = self._call(self.prepared_command(["squeue", "--noheader", "--me",
                "--name=" + name, "--format=%i|%j"]))
            history = self._call(self.prepared_command(["sacct", "--noheader", "--parsable2",
                "--name=" + name, "--starttime=" + since, "--format=JobIDRaw,JobName%80"]))
            jobs = set()
            for line in (queue + "\n" + history).splitlines():
                fields = line.split("|")
                if len(fields) >= 2 and fields[1].strip() == name and re.fullmatch(r"[1-9]\d{0,17}", fields[0].strip()):
                    jobs.add(fields[0].strip())
            if len(jobs) > 1:
                raise ValueError("More than one scheduler job matches this submission. Inspect these jobs before continuing: " + ", ".join(sorted(jobs)))
            if not jobs:
                return {"status": "not_found", "job_name": name}
            job = jobs.pop()
        receipt = {"job_id": job, "operation": operation, "host": self.profile.host,
                   "batch_id": self.profile.batch_id}
        if operation in STAGE_OPERATIONS:
            receipt["result_file"] = str(PurePosixPath(self.profile.working_directory) / (".mif-annotations-" + token + ".json"))
        else:
            receipt["log_pattern"] = str(PurePosixPath(self.profile.working_directory) / "mif-%j.log")
        if operation == "resume" and "resume_from_state_head_sha256" in pending:
            receipt["resume_from_state_head_sha256"] = pending["resume_from_state_head_sha256"]
        return {"status": "found", "receipt": receipt}

    @staticmethod
    def _job(value):
        if not isinstance(value, str) or not re.fullmatch(r"[1-9]\d{0,17}", value):
            raise ValueError("Select a valid scheduler job identifier")
        return value

    def status(self, job_id):
        job = self._job(job_id)
        queue = self._call(
            self.prepared_command(["squeue", "--noheader", "--jobs=" + job, "--format=%i|%T|%M|%L"])
        )
        if not queue:
            queue = self._call(
                self.prepared_command(
                    [
                        "sacct",
                        "--noheader",
                        "--parsable2",
                        "--jobs=" + job,
                        "--format=JobID,State,Elapsed,ExitCode",
                    ]
                )
            )
        return {"job_id": job, "scheduler": queue or "No scheduler record is currently available."}

    def cancel(self, job_id):
        job = self._job(job_id)
        self._call(self.prepared_command(["scancel", job]))
        return {"job_id": job, "cancel_requested": True}

    def prepare_analysis(self, samples):
        value = {
            "schema_version": "cluster-analysis-setup/v2",
            "batch_id": self.profile.batch_id,
            "output_root": self.profile.output_root,
            "manifest": self.profile.manifest,
            "cpus": self.profile.cpus,
            "memory_gib": self.profile.memory_gib,
            "samples": samples,
        }
        payload = json.dumps(value, sort_keys=True, allow_nan=False)
        if len(payload.encode()) > 1024 * 1024:
            raise ValueError("This analysis setup is too large.")
        text = self._call(
            self.prepared_command([self.profile.batch_executable, "prepare-stdin"]), input=payload
        )
        result = json.loads(text)
        if (
            not isinstance(result, dict)
            or result.get("manifest") != self.profile.manifest
            or result.get("batch_id") != self.profile.batch_id
        ):
            raise ValueError("The prepared cluster analysis differs from this setup.")
        return result

    def review_page(self, query=None):
        from .cluster.cluster_review_pages import decode_page
        from .batch_interactions import BatchInteractionQuery

        if query is not None and type(query) is not BatchInteractionQuery:
            raise TypeError("Select a current page of pending reviews.")
        command = self.batch_command("review-page")
        if query is not None:
            command.extend(["--offset", str(query.offset), "--limit", str(query.limit)])
            for value in query.validation_domain_ids:
                command.extend(["--domain", value])
            for value in query.interaction_reasons:
                command.extend(["--reason", value])
        raw = self._call(self.prepared_command(command))
        result = decode_page(json.loads(raw))
        if result.batch_id != self.profile.batch_id:
            raise ValueError("The returned review belongs to another analysis.")
        return result

    def submit_review(self, request):
        from .batch_interactions import BatchInteractionResponseRequest
        from .cluster.cluster_review_pages import _plain

        if type(request) is not BatchInteractionResponseRequest:
            raise TypeError("Select the current displayed review before submitting.")
        payload = json.dumps(_plain(request), sort_keys=True, allow_nan=False)
        if len(payload.encode()) > 1024 * 1024:
            raise ValueError("Review response is too large.")
        # Exit 1 carries a validated rejection outcome; the review UI retains
        # entered values and can display that outcome without a transport error.
        raw = self._call(
            self.prepared_command(self.batch_command("respond-stdin")),
            input=payload, accepted_exit_codes=(0, 1),
        )
        result = json.loads(raw)
        if not isinstance(result, dict) or "applications" not in result:
            raise ValueError("Cluster response did not identify the saved review result.")
        return result

    def review_figures(self, request_id, head, *, attribute="cluster_id"):
        if (
            not isinstance(request_id, str)
            or not re.fullmatch("[A-Za-z0-9_-]{1,160}", request_id)
            or not re.fullmatch("[0-9a-f]{64}", head)
        ):
            raise ValueError("Select a current review before requesting figures.")
        from .cluster.cluster_annotation_jobs import _command

        return json.loads(
            self._call(
                self.prepared_command(_command(self, "review-figures", request_id, head,
                    "--attribute", attribute)),
                max_output_bytes=9 * 1024 * 1024,
            )
        )

    def submit_alignment(self, request, options):
        from .cluster.cluster_alignment import submit

        return submit(self, request, options)

    def describe_analysis_folder(self, folder):
        folder = PurePosixPath(folder)
        if not folder.is_absolute() or ".." in folder.parts:
            raise ValueError("Choose an absolute cluster analysis folder.")
        command = [self.profile.batch_executable, "describe-analysis", str(folder)]
        text = self._call(self.prepared_command(command), max_output_bytes=8 * 1024 * 1024)

        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("Repeated saved-analysis field.")
                value[key] = item
            return value

        return json.loads(
            text,
            object_pairs_hook=unique,
            parse_constant=lambda _v: (_ for _ in ()).throw(
                ValueError("Invalid analysis setup number.")
            ),
        )

    def submit_landmark_export(self, request):
        from .cluster.cluster_landmarks import submit_export

        return submit_export(self, request)

    def submit_landmark_import(self, value):
        from .cluster.cluster_landmarks import submit_import

        return submit_import(self, value)

    def submit_annotation_export(
        self, request_id, expected_head, entry_id=None, *, preview_selection=None, reference_role=None
    ):
        from .cluster.cluster_annotation_jobs import submit_export

        return submit_export(
            self, request_id, expected_head, entry_id,
            preview_selection=preview_selection, reference_role=reference_role,
        )

    def submit_annotation_import(self, ticket_id, objects):
        from .cluster.cluster_annotation_jobs import submit_import

        return submit_import(self, ticket_id, objects)

    def annotation_result(self, receipt):
        from .cluster.cluster_annotation_jobs import result

        return result(self, receipt)

    def resume_readiness(self):
        from .cluster.cluster_annotation_jobs import submission_name
        state = self.analysis_status()
        if (
            not isinstance(state, dict)
            or state.get("batch_id") != self.profile.batch_id
            or not isinstance(state.get("state_head_sha256"), str)
            or not re.fullmatch("[0-9a-f]{64}", state["state_head_sha256"])
        ):
            raise ValueError(
                "The cluster did not return the current analysis status. Refresh before resuming."
            )
        queued = self._call(
            self.prepared_command(
                [
                    "squeue",
                    "--noheader",
                    "--me",
                    "--format=%i|%j|%T",
                ]
            )
        )
        queued = "\n".join(line for line in queued.splitlines()
            if len(line.split("|")) >= 3 and line.split("|")[1].strip().startswith(submission_name(self)))
        allowed = state.get("resume_disposition") == "paused_resumable" and not queued.strip()
        reason = (
            "A job for this analysis is queued or running. Refresh progress before submitting another."
            if queued.strip()
            else (
                "The analysis is paused and can be resumed."
                if allowed
                else "The analysis is not at a resumable pause. Review its current progress and pending inputs."
            )
        )
        return {
            "can_resume": allowed,
            "state_head_sha256": state["state_head_sha256"],
            "reason": reason,
        }

    def analysis_status(self):
        text = self._call(self.prepared_command(self.batch_command("status")))
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(
                "Remote analysis status was not valid JSON; inspect the scheduler log"
            ) from e

    def submit_stage_operation(self, operation, *, node_id=None, expected_head=None,
                               settings=None, terminal_record=None, source_image=None, token=None,
                               review_request_id=None, plot_options=None):
        """Submit the application's saved-step controls through its normal allocation."""
        from .cluster.cluster_annotation_jobs import STAGE_OPERATIONS, _command, _submit
        from .batch_stage_options_transport import settings_mapping
        if operation not in STAGE_OPERATIONS:
            raise ValueError("Choose an available saved-step operation.")
        if operation == "review-figures":
            if (not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", str(review_request_id))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(expected_head))):
                raise ValueError("Select the current cluster review before calculating UMAP.")
            from mif_pipeline.cell_analysis.embeddings import EmbeddingPolicy
            options = dict(plot_options or {})
            policy = EmbeddingPolicy(**options["umap_settings"])
            from dataclasses import asdict
            command = _command(self, operation, review_request_id, expected_head,
                "--umap-settings", json.dumps(asdict(policy), allow_nan=False),
                "--attribute", options.get("attribute", "cluster_id"))
            receipt = _submit(self, operation, command, token=token)
            return {**receipt, "host": self.profile.host, "batch_id": self.profile.batch_id}
        args = []
        if expected_head is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", expected_head):
                raise ValueError("Refresh the saved analysis before changing it.")
            args.extend(["--expected-head-sha256", expected_head])
        if node_id is not None:
            if not re.fullmatch(r"node-[0-9a-f]{48}", node_id):
                raise ValueError("Select a current saved step.")
            args.extend(["--node-id", node_id])
        if terminal_record is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", terminal_record):
                raise ValueError("Select the current failed attempt.")
            args.extend(["--terminal-record-sha256", terminal_record])
        if source_image is not None:
            if operation != "stage-options" or not PurePosixPath(source_image).is_absolute():
                raise ValueError("Choose the source image on the cluster.")
            args.extend(["--source-image", source_image])
        payload = None if settings is None else json.dumps(settings_mapping(settings),
            sort_keys=True, allow_nan=False)
        receipt = _submit(self, operation, _command(self, operation, *args), input_text=payload, token=token)
        return {**receipt, "host": self.profile.host, "batch_id": self.profile.batch_id}

    def stage_operation_result(self, receipt):
        """Poll the retained result without launching another copy of the operation."""
        from .cluster.cluster_annotation_jobs import result
        if receipt.get("host") != self.profile.host or receipt.get("batch_id") != self.profile.batch_id:
            raise ValueError("The saved cluster operation belongs to another analysis.")
        return result(self, {key: receipt[key] for key in ("job_id", "operation", "result_file")})


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Submit or monitor an mIF analysis on an explicitly configured SSH/Slurm cluster"
    )
    parser.add_argument("profile", type=Path)
    parser.add_argument(
        "operation", choices=("run", "resume", "status", "cancel", "analysis-status", "preview")
    )
    parser.add_argument("--job-id")
    args = parser.parse_args(argv)
    try:
        if args.profile.stat().st_size > 65536:
            raise ValueError("Remote profile exceeds its size limit")
        profile = RemoteProfile.from_mapping(json.loads(args.profile.read_text()))
        client = RemoteComputeClient(profile)
        if args.operation == "preview":
            result = {
                "remote_command": shlex.join(client.batch_command("run")),
                "script": client.slurm_script("run"),
                "profile": profile.to_mapping(),
            }
        elif args.operation in {"run", "resume"}:
            result = client.submit(args.operation)
        elif args.operation == "analysis-status":
            result = client.analysis_status()
        else:
            result = getattr(client, args.operation)(args.job_id)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (OSError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
