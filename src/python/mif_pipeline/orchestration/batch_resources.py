"""Checked resource demand arithmetic and bounded scheduler leases.

The `BatchResourceDemand` record carries `node_id`, `adapter_spec_sha256`,
`input_artifact_set_sha256`, `resource_kind`, `worker_slots` and its remaining declared
fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import re
import os
from threading import Lock
from types import MappingProxyType
from uuid import uuid4

import numpy as np

from mif_pipeline.orchestration.batch_dag import ResourceKind
from mif_pipeline.orchestration.batch_manifest import BatchExecutionConfig
from mif_pipeline.pipeline_core import ROIAnalysisError

_MAX_SIGNED_64 = 2**63 - 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


# ==============================================================================
#                                Batch resources
# ==============================================================================


class BatchSchedulerError(ROIAnalysisError):
    """Invalid scheduler authority, demand, dispatch, or persisted result."""


def validate_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise BatchSchedulerError(f"{field_name} must be a lowercase SHA-256")
    return value


def _positive_i64(value: object, field_name: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_SIGNED_64:
        raise BatchSchedulerError(f"{field_name} must be a positive signed-64-bit integer")
    return value


def _checked_product(values: tuple[int, ...], field_name: str) -> int:
    result = 1
    for value in values:
        factor = _positive_i64(value, field_name)
        if result > _MAX_SIGNED_64 // factor:
            raise BatchSchedulerError(f"{field_name} exceeds positive signed-64-bit range")
        result *= factor
    return result


def _checked_add(left: int, right: int, field_name: str) -> int:
    if type(left) is not int or not 0 <= left <= _MAX_SIGNED_64:
        raise BatchSchedulerError(f"{field_name} has an invalid reserved signed-64-bit value")
    increment = _positive_i64(right, field_name)
    if left > _MAX_SIGNED_64 - increment:
        raise BatchSchedulerError(f"{field_name} exceeds positive signed-64-bit range")
    return left + increment


def estimate_working_set_bytes(
    *,
    chunk: tuple[int, ...],
    bands: int,
    dtype: object,
    copies: int,
) -> int:
    """Estimate bounded chunk memory with checked positive integer products."""

    try:
        dimensions = tuple(chunk)
    except TypeError as exc:
        raise BatchSchedulerError("chunk must contain positive dimensions") from exc
    if not dimensions:
        raise BatchSchedulerError("chunk must contain positive dimensions")
    checked_dimensions = tuple(_positive_i64(value, "chunk dimension") for value in dimensions)
    checked_bands = _positive_i64(bands, "bands")
    checked_copies = _positive_i64(copies, "copies")
    try:
        item_size = np.dtype(dtype).itemsize
    except (TypeError, ValueError) as exc:
        raise BatchSchedulerError("dtype must have a fixed positive item size") from exc
    checked_item_size = _positive_i64(item_size, "dtype item size")
    return _checked_product(
        (*checked_dimensions, checked_bands, checked_item_size, checked_copies),
        "working-set product",
    )


@dataclass(frozen=True, slots=True)
class BatchResourceDemand:
    """One exact node/input demand used before attempt authorization."""

    node_id: str
    adapter_spec_sha256: str
    input_artifact_set_sha256: str
    resource_kind: ResourceKind
    worker_slots: int
    future_slots: int
    working_set_bytes: int
    cpu_threads: int = 1
    gpu_sessions: int = 0
    use_available_memory: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or not self.node_id:
            raise BatchSchedulerError("resource demand requires node_id")
        object.__setattr__(
            self,
            "adapter_spec_sha256",
            validate_sha256(self.adapter_spec_sha256, "adapter_spec_sha256"),
        )
        object.__setattr__(
            self,
            "input_artifact_set_sha256",
            validate_sha256(
                self.input_artifact_set_sha256,
                "input_artifact_set_sha256",
            ),
        )
        try:
            resource_kind = ResourceKind(self.resource_kind)
        except (TypeError, ValueError) as exc:
            raise BatchSchedulerError("resource demand kind is not closed") from exc
        object.__setattr__(self, "resource_kind", resource_kind)
        for field_name in (
            "worker_slots",
            "future_slots",
            "working_set_bytes",
            "cpu_threads",
        ):
            object.__setattr__(
                self,
                field_name,
                _positive_i64(getattr(self, field_name), field_name),
            )
        if type(self.gpu_sessions) is not int or self.gpu_sessions not in (0, 1):
            raise BatchSchedulerError("gpu_sessions must be zero or one")
        if type(self.use_available_memory) is not bool:
            raise BatchSchedulerError("use_available_memory must be a boolean")


@dataclass(frozen=True, slots=True)
class ResourceObservation:
    physical_bytes: int
    available_bytes: int
    resident_bytes: int
    cpu_threads: int
    job_memory_bytes: int | None = None


def observe_resources() -> ResourceObservation:
    """Read system availability and a conservative process-tree RSS bound.

    RSS can count shared pages twice across processes; it is intentionally an
    upper bound, never a reason to admit more memory than is available.
    """
    import psutil
    memory = psutil.virtual_memory()
    process = psutil.Process()
    resident = process.memory_info().rss
    for child in process.children(recursive=True):
        try:
            resident += child.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    try:
        cpus = len(os.sched_getaffinity(0))
    except AttributeError:
        cpus = os.cpu_count() or 1
    job_cpus = os.environ.get("SLURM_CPUS_PER_TASK")
    if job_cpus:
        cpus = min(cpus, _positive_i64(int(job_cpus), "allocated CPU threads"))
    job_memory = os.environ.get("MIF_JOB_MEMORY_BYTES")
    if job_memory:
        limit = _positive_i64(int(job_memory), "job memory bytes")
    elif os.environ.get("SLURM_MEM_PER_NODE"):
        limit = _positive_i64(int(os.environ["SLURM_MEM_PER_NODE"]), "job memory MiB") * 1024**2
    elif os.environ.get("SLURM_MEM_PER_CPU"):
        limit = _positive_i64(int(os.environ["SLURM_MEM_PER_CPU"]), "job memory MiB per CPU") * cpus * 1024**2
    else:
        limit = None
    return ResourceObservation(int(memory.total), int(memory.available), resident, cpus, limit)



def fixed_working_set_allowance(node_kind, resource_kind):
    """Shared runtime allowance for tasks without an input-dependent array budget."""
    if node_kind in {"segmentation", "primary_classifier_inference",
            "secondary_classifier_inference", "supervised_phenotype_inference",
            "primary_classifier_training_review", "secondary_classifier_training_review",
            "supervised_phenotype_training_review"}:
        # Fitting and inference retain native models alongside their JSON trees.
        # Use the direct-native allowance; admission still checks free RAM.
        return 2 * 1024 ** 3
    if resource_kind in {ResourceKind.QUPATH_IMAGE_READ, ResourceKind.QUPATH_IMAGE_WRITE,
            ResourceKind.PROJECT_CATALOGUE_MUTATION, ResourceKind.SHARED_CLASSIFIER_MUTATION,
            ResourceKind.BIOFORMATS_CONVERSION}:
        return 1024 ** 3
    return 256 * 1024 ** 2


def available_working_memory(observed, *, baseline_resident=None, reserved_memory=0):
    """Reserve OS headroom and count only unmaterialized leases beyond current RSS.

    Host availability already excludes resident processes. A cluster allocation
    additionally bounds this process tree plus its outstanding reservations.
    """
    baseline = observed.resident_bytes if baseline_resident is None else baseline_resident
    reserved_growth = max(0, baseline + reserved_memory - observed.resident_bytes)
    available = observed.available_bytes * 4 // 5 - reserved_growth
    if observed.job_memory_bytes is not None:
        available = min(available, observed.job_memory_bytes * 4 // 5
                        - max(baseline + reserved_memory, observed.resident_bytes))
    return max(0, available)


def resource_memory_wait_reason(working_set_bytes, observed, *, baseline_resident=None, reserved_memory=0):
    """Explain the live memory bounds used by admission without reserving work."""
    available = available_working_memory(observed, baseline_resident=baseline_resident,
                                         reserved_memory=reserved_memory)
    if working_set_bytes <= available:
        return None
    gib = 1024 ** 3
    baseline = observed.resident_bytes if baseline_resident is None else baseline_resident
    required = max(baseline + reserved_memory, observed.resident_bytes) + working_set_bytes
    if observed.job_memory_bytes is not None and required > observed.job_memory_bytes * 4 // 5:
        action = "Request more memory for the cluster job; processing can use up to 80% of that allocation."
    else:
        action = "Wait for other processing to finish or close applications that are using memory."
    return (f"This step needs an estimated {working_set_bytes / gib:.2f} GiB of working memory; "
            f"{available / gib:.2f} GiB is currently available for it. "
            + action + " Then select Continue saved work.")


class ResourceLease:
    """One releasable reservation issued by a ResourceLeasePool."""

    __slots__ = ("_pool", "_token", "demand", "_released")

    def __init__(self, pool: "ResourceLeasePool", token: str, demand: BatchResourceDemand) -> None:
        self._pool = pool
        self._token = token
        self.demand = demand
        self._released = False

    @property
    def is_live(self) -> bool:
        return not self._released

    def release(self) -> None:
        if self._released:
            raise BatchSchedulerError("resource lease was already released")
        self._pool._release(self._token, self.demand)
        self._released = True


class ResourceLeasePool:
    """Thread-safe bounded reservations for workers, futures, memory, and kind."""

    def __init__(self, config: BatchExecutionConfig, *, observer=observe_resources) -> None:
        if not isinstance(config, BatchExecutionConfig):
            raise BatchSchedulerError("resource pool requires BatchExecutionConfig")
        self._wait_reasons = {}
        self._max_workers = _positive_i64(config.max_workers, "max_workers")
        self._max_futures = min(2, _positive_i64(config.max_in_flight, "max_in_flight"))
        capacities: dict[ResourceKind, int] = {}
        for kind in ResourceKind:
            try:
                raw = config.resource_capacities[kind.value]
            except KeyError as exc:
                raise BatchSchedulerError(f"resource capacity {kind.value!r} is missing") from exc
            capacities[kind] = _positive_i64(raw, f"resource capacity {kind.value}")
        self._capacities = MappingProxyType(capacities)
        self._workers = 0
        self._futures = 0
        self._memory = 0
        self._cpu = 0
        self._gpu = 0
        self._observer = observer
        self._baseline_resident = observer().resident_bytes
        self._by_kind = {kind: 0 for kind in ResourceKind}
        self._leases: dict[str, BatchResourceDemand] = {}
        self._lock = Lock()

    def _fits_total(self, demand: BatchResourceDemand) -> bool:
        return (
            demand.worker_slots <= self._max_workers
            and demand.future_slots <= self._max_futures
            and self._capacities[demand.resource_kind] >= 1
        )

    def try_acquire(self, demand: BatchResourceDemand) -> ResourceLease | None:
        if not isinstance(demand, BatchResourceDemand):
            raise BatchSchedulerError("resource acquisition requires typed demand")
        if not self._fits_total(demand):
            raise BatchSchedulerError(
                f"resource demand for {demand.node_id!r} exceeds scheduler capacity"
            )
        with self._lock:
            observed = self._observer()
            if demand.cpu_threads > observed.cpu_threads:
                raise BatchSchedulerError(
                    f"This step requires {demand.cpu_threads} CPU threads; "
                    f"{observed.cpu_threads} are available. Make at least "
                    f"{demand.cpu_threads} CPU threads available to processing, "
                    "then select Continue saved work."
                )
            workers = _checked_add(self._workers, demand.worker_slots, "reserved worker slots")
            futures = _checked_add(self._futures, demand.future_slots, "reserved future slots")
            memory = _checked_add(
                self._memory,
                demand.working_set_bytes,
                "reserved working-set bytes",
            )
            resource_slots = _checked_add(
                self._by_kind[demand.resource_kind],
                1,
                f"reserved {demand.resource_kind.value} slots",
            )
            available = available_working_memory(observed,
                baseline_resident=self._baseline_resident, reserved_memory=self._memory)
            if (
                workers > self._max_workers
                or futures > self._max_futures
                or resource_slots > self._capacities[demand.resource_kind]
                or self._cpu + demand.cpu_threads > observed.cpu_threads
                or self._gpu + demand.gpu_sessions > 1
                or demand.working_set_bytes > available
            ):
                self._wait_reasons[demand.node_id] = resource_memory_wait_reason(
                    demand.working_set_bytes, observed, baseline_resident=self._baseline_resident,
                    reserved_memory=self._memory) or "Waiting for another admitted task to release its processor or worker slot."
                return None
            if demand.use_available_memory:
                # Conventional model JSON has no reliable array-size
                # estimate. Grant unreserved RAM within the same host/job bound;
                # later work waits for this lease instead of sharing its budget.
                quantum = 64 * 1024**2
                demand = replace(demand, working_set_bytes=max(
                    demand.working_set_bytes, available // quantum * quantum))
                memory = _checked_add(self._memory, demand.working_set_bytes,
                                      "reserved working-set bytes")
            self._wait_reasons.pop(demand.node_id, None)
            token = uuid4().hex
            self._workers = workers
            self._futures = futures
            self._memory = memory
            self._cpu += demand.cpu_threads
            self._gpu += demand.gpu_sessions
            self._by_kind[demand.resource_kind] = resource_slots
            self._leases[token] = demand
        return ResourceLease(self, token, demand)

    def _release(self, token: str, demand: BatchResourceDemand) -> None:
        with self._lock:
            if self._leases.get(token) is not demand:
                raise BatchSchedulerError("resource lease is stale or foreign")
            self._leases.pop(token)
            self._workers -= demand.worker_slots
            self._futures -= demand.future_slots
            self._memory -= demand.working_set_bytes
            self._cpu -= demand.cpu_threads
            self._gpu -= demand.gpu_sessions
            self._by_kind[demand.resource_kind] -= 1

    @property
    def wait_reasons_by_node(self):
        with self._lock:
            return MappingProxyType(dict(self._wait_reasons))

    @property
    def reserved_worker_slots(self) -> int:
        with self._lock:
            return self._workers

    @property
    def reserved_future_slots(self) -> int:
        with self._lock:
            return self._futures

    @property
    def reserved_working_set_bytes(self) -> int:
        with self._lock:
            return self._memory

    @property
    def reserved_resource_slots(self) -> Mapping[ResourceKind, int]:
        with self._lock:
            return MappingProxyType(dict(self._by_kind))


__all__ = [
    "BatchResourceDemand",
    "BatchSchedulerError",
    "ResourceLease",
    "ResourceLeasePool",
    "estimate_working_set_bytes",
]
