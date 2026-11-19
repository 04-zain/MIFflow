"""One admitted operation's memory, cache and native thread allowances."""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
import os
import re
import sys
from threading import Lock


@dataclass(frozen=True, slots=True)
class OperationBudget:
    memory_bytes: int
    cpu_threads: int
    native_memory_bytes: int | None = None


_current_budget: ContextVar[OperationBudget | None] = ContextVar("mif_operation_budget", default=None)
_numerical_lock = Lock()
_numerical_users = 0
_numerical_limiters = []


def current_operation_budget() -> OperationBudget | None:
    return _current_budget.get()


@contextmanager
def numerical_thread_limit():
    """Cap loaded numerical pools and restore their original thread settings.

    Enter again after importing an optional numerical backend so its newly
    loaded libraries also receive the limit. BLAS settings are process-wide:
    retain prior settings until the last user exits. OpenMP settings belong to
    the calling thread and are restored by that same thread on leaving.
    """
    global _numerical_users
    with _numerical_lock:
        from threadpoolctl import threadpool_limits
        _numerical_limiters.append(threadpool_limits(limits=1, user_api='blas'))
        _numerical_users += 1
    try:
        with threadpool_limits(limits=1, user_api='openmp'):
            yield
    finally:
        with _numerical_lock:
            _numerical_users -= 1
            if _numerical_users == 0:
                for limiter in reversed(_numerical_limiters):
                    limiter.restore_original_limits()
                _numerical_limiters.clear()


@contextmanager
def operation_budget(memory_bytes: int, cpu_threads: int, *, native_memory_bytes: int | None = None):
    if memory_bytes <= 0 or cpu_threads <= 0:
        raise ValueError("An operation requires positive memory and CPU allowances")
    if native_memory_bytes is not None and not 0 < native_memory_bytes <= memory_bytes:
        raise ValueError("Native memory must fit the complete operation allowance")
    token = _current_budget.set(OperationBudget(memory_bytes, cpu_threads, native_memory_bytes))
    try:
        with numerical_thread_limit():
            yield
    finally:
        _current_budget.reset(token)


def native_library_environment(inherited: Mapping[str, str]) -> dict[str, str]:
    """Supply installed CPU libraries or the complete reusable NVIDIA library set.

    Java uses the CPU directory when the NVIDIA library set is not installed.
    Explicit PyTorch runtime selections retain precedence. Setup checks the
    library installation; launching does not repeat its hashes.
    """
    environment = dict(inherited)
    if "MIF_CPU_NATIVE_ROOT" not in environment and sys.platform in {"linux", "win32"}:
        from mif_pipeline.orchestration.setup.prerequisite_setup import default_cpu_setup_target
        platform = "win-x86_64" if sys.platform == "win32" else "linux-x86_64"
        environment["MIF_CPU_NATIVE_ROOT"] = str(default_cpu_setup_target(platform, environ=environment))
    from mif_pipeline.orchestration.setup.native_gpu_setup import native_gpu_environment
    return native_gpu_environment(environment)


def native_compute_environment(
    inherited: dict[str, str], *, kernel_threads: int | None = None
) -> dict[str, str]:
    """Bind subprocess pools and caches before Java/native libraries initialize.

    Cache allowances are slices of the admitted process memory, not additional
    reservations. Java heap and direct memory together leave room for native
    model tensors, codecs and interpreter/process overhead.
    Tile-parallel stages can keep each numerical kernel serial while retaining
    their admitted CPU allowance for the tile workers.
    """
    environment = native_library_environment(inherited)
    budget = current_operation_budget()
    if budget is None:
        # Direct launches retain their per-operation allowance while using
        # the same live host/job headroom as batch admission.
        from mif_pipeline.orchestration.batch_resources import observe_resources, available_working_memory
        observed = observe_resources()
        allowance = min(2 * 1024**3, available_working_memory(observed))
        if allowance < 1024**3:
            raise RuntimeError(
                "Native computation requires at least 1 GiB of available working memory; "
                f"the current host/job memory bounds allow {max(0, allowance)} bytes"
            )
        budget = OperationBudget(allowance, min(2, observed.cpu_threads))
    memory, threads = budget.native_memory_bytes or budget.memory_bytes, budget.cpu_threads
    if kernel_threads is not None and (
        type(kernel_threads) is not int or not 1 <= kernel_threads <= threads
    ):
        raise ValueError("Native kernel threads must fit the operation's CPU allowance")
    numerical_threads = threads if kernel_threads is None else kernel_threads
    caches = memory // 5
    environment.update({
        "MIF_COMPUTE_MEMORY_BYTES": str(budget.memory_bytes),
        "MIF_COMPUTE_CPU_THREADS": str(threads),
        "MIF_DEVICE_MEMORY_BYTES": str(budget.memory_bytes),
        "MIF_IMAGE_CACHE_BYTES": str(caches // 2),
        "MIF_OBJECT_CACHE_BYTES": str(caches * 36 // 100),
        "MIF_FEATURE_CACHE_BYTES": str(caches * 4 // 100),
        "MIF_METADATA_CACHE_BYTES": str(caches // 10),
    })
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                 "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        environment[name] = str(numerical_threads)
    # DJL inter-op and intra-op pools are independent. Use one inter-op worker;
    # reserve its CPU alongside the selected intra-op parallelism.
    intra = max(1, threads - 1) if kernel_threads is None else kernel_threads
    quantum = 16 * 1024**2
    limits = " ".join((
        f"-Xmx{memory * 3 // 5 // quantum * quantum}", f"-XX:MaxDirectMemorySize={memory // 5 // quantum * quantum}",
        "-XX:+ExitOnOutOfMemoryError",
        f"-XX:ActiveProcessorCount={threads}",
        "-Dai.djl.pytorch.num_interop_threads=1", f"-Dai.djl.pytorch.num_threads={intra}"))
    for key in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"):
        options = environment.get(key, "")
        options = re.sub(r"(?<!\S)(?:-Xmx\S+|-XX:[+-]ExitOnOutOfMemoryError|-XX:(?:ActiveProcessorCount|MaxDirectMemorySize|MaxRAMPercentage|MaxHeapSize)=\S+|-Dai\.djl\.pytorch\.(?:num_threads|num_interop_threads)=\S+)(?=\s|$)", "", options)
        if options.strip() or key in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS"):
            # HotSpot reads _JAVA_OPTIONS last, including after jpackage's
            # configured JVM options; a launcher cannot enlarge this budget.
            environment[key] = " ".join((options.strip(), limits if key != "JDK_JAVA_OPTIONS" else "")).strip()
    return environment
