"""Seed one published generation's isolated native QuPath preferences.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import json
import math
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile

from .launch_binding import QuPathProfileLaunchBinding, qupath_command
from .managed_profile import ManagedQuPathProfileError, load_managed_qupath_profile
from .prerequisite_probe import (
    ProbeProcessRunner,
    _PROBE_LIMIT,
    _closed_object,
    _is_link,
    _probe_environment,
    _run_bounded_probe,
    _unique_object,
)

# ==============================================================================
#                                 Profile setup
# ==============================================================================


def seed_managed_qupath_profile(
    binding: QuPathProfileLaunchBinding,
    *,
    process_runner: ProbeProcessRunner | None = None,
    timeout_seconds: float = 15.0,
) -> None:
    """Set only the verified generation's user path; grant no readiness authority.

    The caller must observe class origins in a separate fresh process after
    this returns. A failure leaves the published generation unselected.
    """

    if type(binding) is not QuPathProfileLaunchBinding:
        raise ManagedQuPathProfileError("profile seeding requires an exact launch binding")
    if (
        type(timeout_seconds) not in (int, float)
        or not 0 < timeout_seconds <= 30
        or not math.isfinite(timeout_seconds)
    ):
        raise ManagedQuPathProfileError("seed timeout must be finite and within (0, 30]")
    if process_runner is not None and not callable(process_runner):
        raise ManagedQuPathProfileError("profile seed process runner must be callable")
    try:
        command_prefix = qupath_command(binding.executable, launch_binding=binding)
        current = load_managed_qupath_profile(
            binding.profile_root / "manifest.json",
            expected_executable=binding.executable,
        )
        if current != binding:
            raise ManagedQuPathProfileError(
                "published managed profile differs from the seed binding"
            )
        with tempfile.TemporaryDirectory(prefix="qupath-profile-seed-") as directory:
            exchange = Path(directory).resolve()
            script = exchange / "seed.groovy"
            from .scripts import internal_probe_bytes
            script.write_bytes(internal_probe_bytes("general/05_seed_managed_profile.groovy"))
            request_path = exchange / "request.json"
            evidence_path = exchange / "evidence.json"
            request = {
                "schema": "qupath-profile-seed/v1",
                "challenge": secrets.token_hex(32),
                "qupath_version": "0.7.0",
                "preferences_namespace": "/" + binding.preferences_namespace,
                "profile_root": str(binding.profile_root),
            }
            encoded_request = json.dumps(request, ensure_ascii=False, allow_nan=False).encode(
                "utf-8"
            )
            if len(encoded_request) > _PROBE_LIMIT:
                raise ManagedQuPathProfileError("profile seed request exceeds its limit")
            request_path.write_bytes(encoded_request)
            command = (
                *command_prefix,
                "script",
                "--args",
                str(request_path),
                "--args",
                str(evidence_path),
                str(script),
            )
            completed = (process_runner or _run_bounded_probe)(
                command,
                env=_probe_environment(exchange, exchange),
                timeout=timeout_seconds,
            )
            if (
                type(completed.returncode) is not int
                or completed.returncode != 0
                or any(
                    type(stream) is not bytes or len(stream) > _PROBE_LIMIT
                    for stream in (completed.stdout, completed.stderr)
                )
            ):
                raise ManagedQuPathProfileError(
                    "profile seed process failed or exceeded its output limit"
                )
            if _is_link(evidence_path) or not stat.S_ISREG(evidence_path.lstat().st_mode):
                raise ManagedQuPathProfileError("profile seed response is not a safe file")
            with evidence_path.open("rb") as stream:
                encoded = stream.read(_PROBE_LIMIT + 1)
            if len(encoded) > _PROBE_LIMIT:
                raise ManagedQuPathProfileError("profile seed response exceeds its limit")
            evidence = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(
                    ValueError("nonfinite seed response")
                ),
            )
            _closed_object(evidence, {*request, "previous_user_path", "saved"})
            if (
                any(evidence[key] != value for key, value in request.items())
                or evidence["saved"] is not True
            ):
                raise ManagedQuPathProfileError(
                    "profile seed response differs from the requested generation"
                )
            previous = evidence["previous_user_path"]
            if previous is not None and (
                type(previous) is not str
                or (previous.strip() and previous != request["profile_root"])
            ):
                raise ManagedQuPathProfileError(
                    "profile namespace already names a foreign user path"
                )
    except ManagedQuPathProfileError:
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        RecursionError,
        subprocess.SubprocessError,
    ) as exc:
        raise ManagedQuPathProfileError(
            "managed QuPath profile seed could not be confirmed"
        ) from exc


__all__ = ["seed_managed_qupath_profile"]
