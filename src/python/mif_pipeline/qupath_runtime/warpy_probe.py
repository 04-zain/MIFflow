"""Pinned native Warpy transform check independent of segmentation libraries."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from dataclasses import dataclass
from pathlib import Path
import hashlib, json, secrets, tempfile
from .launch_binding import qupath_command
from . import payload_lock
from .prerequisite_probe import (
    ProbeFileExpectation,
    QuPathProbeFinding,
    QuPathPrerequisiteProbeResult,
    _absolute_clean_path,
    _file_check,
    _run_bounded_probe,
    _probe_environment,
    _PROBE_LIMIT,
    _unique_object,
)

# ==============================================================================
#                                  Warpy probe
# ==============================================================================


@dataclass(frozen=True, slots=True)
class WarpyPrerequisiteRequest:
    executable: Path
    extension_jar: Path
    launch_binding: object = None

    @property
    def model_root(self):
        return None

    @property
    def model_version(self):
        return None

    @property
    def require_warpy(self):
        return True

    @property
    def warpy_jar(self):
        return ProbeFileExpectation(self.extension_jar, payload_lock.WARPY_SOURCE_ARCHIVE[2])

    def __post_init__(self):
        _absolute_clean_path(self.executable, "QuPath program")
        _absolute_clean_path(self.extension_jar, "Warpy extension")
        qupath_command(self.executable, launch_binding=self.launch_binding)


def probe_warpy_prerequisites(request, *, process_runner=None):
    if type(request) is not WarpyPrerequisiteRequest:
        raise TypeError("Expected a Warpy-only prerequisite request")
    unused = QuPathProbeFinding(
        "missing", "Not checked: this request is for paired-image registration."
    )

    def failed(status, detail):
        finding = QuPathProbeFinding(status, detail)
        return QuPathPrerequisiteProbeResult(finding, unused, None, unused, finding)

    check = _file_check(request.warpy_jar)
    if check.status != "verified":
        return failed(check.status, check.diagnostic)
    try:
        from mif_pipeline.registration.automatic import find_elastix_directory
        from .elastix_runtime import check_elastix_programs
        programs = find_elastix_directory()
        program_detail = check_elastix_programs(programs, process_runner=process_runner)
        with tempfile.TemporaryDirectory(prefix="warpy-check-") as directory:
            root = Path(directory)
            source = root / "request.json"
            result = root / "result.json"
            script = root / "probe.groovy"
            from .scripts import internal_probe_bytes
            script.write_bytes(internal_probe_bytes("general/06_probe_warpy.groovy"))
            challenge = secrets.token_hex(32)
            payload = {
                "schema": "warpy-runtime-check/v1",
                "challenge": challenge,
                "extension": str(request.extension_jar),
                "sha256": request.warpy_jar.sha256,
                "version": "0.5.0",
            }
            source.write_text(json.dumps(payload))
            command = qupath_command(
                request.executable,
                "script",
                "--args",
                str(source),
                "--args",
                str(result),
                str(script),
                launch_binding=request.launch_binding,
            )
            done = (process_runner or _run_bounded_probe)(
                command, env=_probe_environment(root, root), timeout=30.0
            )
            if (
                done.returncode != 0
                or not result.is_file()
                or result.is_symlink()
                or result.stat().st_size > _PROBE_LIMIT
            ):
                return failed(
                    "error",
                    "QuPath could not execute the Warpy transform check. Check the selected extension and restart QuPath.",
                )
            encoded = result.read_bytes()
            value = json.loads(encoded, object_pairs_hook=_unique_object)
            if value != {**payload, "qupath_version": "0.7.0", "transform_checked": True}:
                return failed(
                    "incompatible",
                    "The loaded Warpy version or transformation check differs from this installation.",
                )
            if _file_check(request.warpy_jar).status != "verified":
                return failed("incompatible", "The Warpy extension changed during its check.")
            digest = hashlib.sha256(encoded).hexdigest()
            return QuPathPrerequisiteProbeResult(
                QuPathProbeFinding("ready", "QuPath runtime checked.", "0.7.0", digest),
                unused,
                None,
                unused,
                QuPathProbeFinding(
                    "ready",
                    "Warpy saved, reopened and applied a native transformation. " + program_detail,
                    "0.5.0",
                    digest,
                ),
            )
    except (OSError, ValueError, RuntimeError) as error:
        return failed("error", "Image-alignment software check could not finish: " + str(error))
