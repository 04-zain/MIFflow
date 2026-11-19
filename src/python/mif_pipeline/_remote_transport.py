"""Exchange bounded numerical tasks with one SSH/Slurm worker session.

The client starts the configured worker, verifies its release and protocol, then sends
length-prefixed JSON metadata and binary array payloads. Each reply is matched to the
request ID and payload checksum. Timeouts, cancellation and a closed stream terminate the
session before another task can consume its reply. The worker interface is limited to the
declared numerical operations."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shlex
import struct
import subprocess
import tempfile
import threading
import time

from mif_pipeline._release import RELEASE_ID, WORKER_PROTOCOL

PROTOCOL = WORKER_PROTOCOL
MAX_HEADER = 4 * 1024 * 1024
MAX_PAYLOAD = 64 * 1024 * 1024
TASKS = frozenset({"graph_training"})


# ==============================================================================
#                                Remote transport
# ==============================================================================


def _read_exact(stream, count):
    result = bytearray()
    while len(result) < count:
        part = stream.read(count - len(result))
        if not part:
            raise EOFError("The remote worker disconnected before completing a result")
        result.extend(part)
    return bytes(result)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate remote header field")
        result[key] = value
    return result


def read_frame(stream):
    prefix = stream.read(1)
    if not prefix:
        return None
    prefix += _read_exact(stream, 3)
    length = struct.unpack(">I", prefix)[0]
    if not 1 <= length <= MAX_HEADER:
        raise ValueError("Remote header exceeds its limit")
    header = json.loads(
        _read_exact(stream, length),
        object_pairs_hook=_unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite remote header")),
    )
    size = header.get("byte_count") if isinstance(header, dict) else None
    if type(size) is not int or not 0 <= size <= MAX_PAYLOAD:
        raise ValueError("Remote payload exceeds its limit")
    payload = _read_exact(stream, size)
    if header.get("payload_sha256") != hashlib.sha256(payload).hexdigest():
        raise ValueError("Remote payload checksum differs")
    return header, payload


def _write_all(stream, payload):
    remaining = memoryview(payload)
    while remaining:
        written = stream.write(remaining)
        if type(written) is not int or written <= 0:
            raise OSError("Remote output stream closed before its frame was written")
        remaining = remaining[written:]


def write_frame(stream, header, payload=b""):
    if type(payload) is not bytes or len(payload) > MAX_PAYLOAD:
        raise ValueError("Remote payload exceeds its limit")
    value = dict(
        header, byte_count=len(payload), payload_sha256=hashlib.sha256(payload).hexdigest()
    )
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_HEADER:
        raise ValueError("Remote header exceeds its limit")
    _write_all(stream, struct.pack(">I", len(raw)))
    _write_all(stream, raw)
    _write_all(stream, payload)
    stream.flush()


def kernel_command(profile):
    if not isinstance(profile, dict) or profile.get("transfer_consent") is not True:
        raise ValueError(
            "Selective remote processing requires explicit consent to transfer numerical data"
        )
    allowed = {
        "host",
        "worker_executable",
        "working_directory",
        "cpus",
        "memory_gib",
        "hours",
        "gpus",
        "partition",
        "account",
        "transfer_consent",
        "tasks",
        "queue_timeout_seconds",
        "task_timeout_seconds",
    }
    if set(profile) - allowed:
        raise ValueError("Unknown selective-compute profile fields")
    host = profile.get("host")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,252}", host):
        raise ValueError("Invalid SSH host")
    for name in ("worker_executable", "working_directory"):
        value = profile.get(name)
        if (
            not isinstance(value, str)
            or not PurePosixPath(value).is_absolute()
            or any(c in value for c in "\0\r\n")
        ):
            raise ValueError("Remote worker and job paths must be absolute cluster paths")
    tasks = profile.get("tasks")
    if (
        not isinstance(tasks, list)
        or not tasks
        or len(tasks) != len(set(tasks))
        or set(tasks) - TASKS
    ):
        raise ValueError("Select supported remote numerical tasks")
    for name, maximum in (("cpus", 256), ("memory_gib", 4096), ("hours", 168), ("gpus", 16)):
        value = profile.get(name, 0 if name == "gpus" else 1)
        if type(value) is not int or not (0 if name == "gpus" else 1) <= value <= maximum:
            raise ValueError("Invalid remote resource: " + name)
    for name in ("partition", "account"):
        value = profile.get(name, "")
        if not isinstance(value, str) or (
            value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value)
        ):
            raise ValueError("Invalid scheduler " + name)
    for name, default in (("queue_timeout_seconds", 300), ("task_timeout_seconds", 900)):
        value = profile.get(name, default)
        if type(value) is not int or not 1 <= value <= 604800:
            raise ValueError("Remote timeout must be a bounded positive integer")
    command = [
        "srun",
        "--nodes=1",
        "--ntasks=1",
        "--kill-on-bad-exit=1",
        "--job-name=mif-selective",
        "--cpus-per-task=" + str(profile.get("cpus", 1)),
        "--mem=" + str(profile.get("memory_gib", 1)) + "G",
        "--time=" + str(profile.get("hours", 1)) + ":00:00",
        "--chdir=" + profile["working_directory"],
    ]
    for key in ("gpus", "partition", "account"):
        if profile.get(key):
            command.append("--" + key + "=" + str(profile[key]))
    command.extend([profile["worker_executable"], "--stdio"])
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=15",
        host,
        shlex.join(command),
    ]


def selected_profile(task):
    if os.environ.get("MIF_REMOTE_WORKER") == "1":
        return None
    location = os.environ.get("MIF_SELECTIVE_COMPUTE_PROFILE")
    if not location:
        return None
    path = Path(location)
    if not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("Selective-compute profile is unavailable or too large")
    profile = json.loads(path.read_text())
    kernel_command(profile)
    return profile if task in profile["tasks"] else None


class RemoteKernelSession:
    """One scheduler-owned worker; only bounded arrays, never project files, cross SSH."""

    def __init__(self, profile, *, command=None):
        self.profile = json.loads(json.dumps(profile))
        self._lock = threading.Lock()
        self._request_id = 0
        self._closed = False
        self.release = RELEASE_ID
        argv = (
            kernel_command(profile) if command is None else command
        )  # Explicit injection only for local transport tests.
        self._log = tempfile.TemporaryFile()
        self._results = queue.Queue(maxsize=2)
        try:
            self.process = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log, bufsize=0
            )
        except BaseException:
            self._log.close()
            raise

        def reader():
            try:
                while not self._closed:
                    frame = read_frame(self.process.stdout)
                    if frame is None:
                        raise EOFError("Remote numerical worker closed its stream")
                    self._results.put(frame, timeout=2)
            except BaseException as error:
                try:
                    self._results.put(error, timeout=2)
                except queue.Full:
                    pass

        self._reader = threading.Thread(target=reader, daemon=True)
        self._reader.start()
        try:
            self.runtime, _ = self.request(
                "probe", {}, timeout=profile.get("queue_timeout_seconds", 300)
            )
        except BaseException:
            self.close()
            raise

    def _wait(self, results, deadline, cancelled):
        while True:
            if cancelled is not None and cancelled():
                raise InterruptedError("Selective remote processing was cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Remote computation or scheduler wait exceeded its limit")
            try:
                return results.get(timeout=min(0.2, remaining))
            except queue.Empty:
                continue

    def request(self, operation, metadata, payload=b"", *, timeout=None, cancelled=None):
        with self._lock:
            if self._closed:
                raise RuntimeError("Remote worker is closed")
            self._request_id += 1
            identity = self._request_id
            header = {
                "protocol": PROTOCOL,
                "request_id": identity,
                "operation": operation,
                "release": self.release,
                "metadata": metadata,
            }
            deadline = time.monotonic() + (timeout or self.profile.get("task_timeout_seconds", 900))
            sent = queue.Queue(maxsize=1)

            def sender():
                try:
                    write_frame(self.process.stdin, header, payload)
                    sent.put(None)
                except BaseException as error:
                    sent.put(error)

            thread = threading.Thread(target=sender, daemon=True)
            thread.start()
            try:
                error = self._wait(sent, deadline, cancelled)
                if error is not None:
                    raise error
                response = self._wait(self._results, deadline, cancelled)
                if isinstance(response, BaseException):
                    raise response
                received, body = response
                if (
                    received.get("protocol") != PROTOCOL
                    or received.get("request_id") != identity
                    or received.get("operation") != operation
                    or received.get("release") != header["release"]
                ):
                    raise ValueError(
                        "Remote result belongs to another request or software revision"
                    )
                if received.get("error"):
                    raise ValueError("Remote task failed: " + str(received["error"])[:4000])
                if not isinstance(received.get("metadata"), dict):
                    raise ValueError("Remote result metadata is invalid")
                return received["metadata"], body
            except BaseException as error:
                self.close()
                if isinstance(error, InterruptedError):
                    raise
                raise RuntimeError(
                    "Selective HPC task did not complete; local accepted results are unchanged. "
                    + str(error)
                ) from error

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout):
            if stream and not stream.closed:
                stream.close()
        self._reader.join(timeout=3)
        self._log.seek(0, 2)
        size = self._log.tell()
        self._log.seek(max(0, size - 4000))
        detail = self._log.read().decode(errors="replace")
        if detail:
            import sys

            print("Remote worker log: " + detail, file=sys.stderr, flush=True)
        self._log.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
