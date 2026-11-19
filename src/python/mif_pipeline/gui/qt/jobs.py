"""

Runs image, model and analysis operations without blocking the Qt interface.

We start each operation on a Python thread and return its completed value
through a queue. A Qt timer checks that queue every 25 milliseconds while
operations remain active. Once an operation's thread has ended, we join it,
remove it from the active list and emit its success or failure on the Qt
application thread. The receiving page can then update its controls safely.

Read-only operations can run together. An analysis run or settings-changing
operation is limited to one at a time, with a separate control call permitted
to request pause or cancellation of the active run. Repeated read requests
can share an existing operation when their caller supplies the same read key.

This module delivers pause/cancel requests to the analysis operation; the
operation implements the actual stopping and cleanup. Worker threads are
non-daemon threads, so closing a page does not discard an unfinished write.
The desktop's shutdown code checks the active-job list before allowing exit.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from queue import Empty, SimpleQueue
from threading import Thread
import traceback
from typing import Literal
from uuid import uuid4

from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer, Signal

# ==============================================================================
#                                      Jobs
# ==============================================================================


class JobAccess(str, Enum):
    """

    Specifies whether an operation reads data, changes analysis settings or controls a run.

    READ can run concurrently with other reads or an ongoing operation.
    MUTATION and RUN are exclusive against another MUTATION, RUN or CONTROL.
    CONTROL is used for pause/cancel and must identify the exact active RUN.
    These categories govern dispatch; they do not inspect the supplied callable
    to determine which files it will read or change.
    """

    READ = "read"
    MUTATION = "mutation"
    RUN = "run"
    CONTROL = "control"


@dataclass(frozen=True, slots=True)
class DesktopJobSuccess:
    """

    Carries a completed operation's value from its Python thread to Qt.

    Attributes:
        job_id: Identifier assigned when the operation started.
        operation: Displayed operation name supplied by the calling page.
        value: Returned numerical, configuration or file-result value. A QObject
            is rejected before creating this result because Qt objects belong
            to their designated Qt thread.
    """

    job_id: str
    operation: str
    value: object


@dataclass(frozen=True, slots=True)
class DesktopJobError:
    """

    Carries the original operation and its error details back to the interface.

    Attributes:
        job_id: Identifier of the operation that failed.
        operation: Displayed name used when that operation was submitted.
        error_type: Exception class name, retained even when message formatting fails.
        message: Text used by the receiving page to explain the failed operation.
        diagnostic: Complete traceback for Details, or the exception type and
            available message when traceback formatting itself fails.
    """

    job_id: str
    operation: str
    error_type: str
    message: str
    diagnostic: str


_JobOutcome = DesktopJobSuccess | DesktopJobError
_FALLBACK_ERROR_TYPE = "BaseException"
_FALLBACK_ERROR_MESSAGE = "The processing error has no additional details."


def _error_type_name(error: BaseException) -> str:
    """

    Reads the exception class name while retaining an error result if that lookup fails.

    Args:
        error: Exception raised by a background operation.

    Returns:
        Nonempty class-name text, or "BaseException" when the type name cannot
        be obtained. This allows the operation to finish its failure report.
    """

    try:
        name = type(error).__name__
    except BaseException:
        return _FALLBACK_ERROR_TYPE
    return name if type(name) is str and name else _FALLBACK_ERROR_TYPE


def _error_message(error: BaseException, error_type: str) -> str:
    """

    Formats the operation's exception as text for the receiving page.

    An exception supplied by another library can define its own __str__ method.
    If that method raises another exception, we retain the original error type
    and state that its message was unavailable rather than losing the job result.

    Args:
        error: Exception raised by the operation.
        error_type: Class name already obtained for that exception.

    Returns:
        Exception text, or an error-type message when text conversion fails.
    """

    try:
        return str(error)
    except BaseException:
        return f"{error_type} message unavailable"


def _error_diagnostic(
    error: BaseException,
    error_type: str,
    message: str,
) -> str:
    """

    Formats the exception and traceback for the error's Details section.

    Args:
        error: Original exception, including its traceback when available.
        error_type: Previously resolved exception class name.
        message: Previously resolved display message.

    Returns:
        Complete traceback text. If traceback formatting fails or produces no
        text, the result contains the exception type and available message.
    """

    try:
        diagnostic = "".join(
            traceback.format_exception(
                type(error),
                error,
                error.__traceback__,
            )
        )
    except BaseException:
        return f"{error_type}: {message}"
    diagnostic = diagnostic or f"{error_type}: {message}"
    if error_type == "QuPathExecutionError" and getattr(error, "record", None) is not None:
        try:
            from mif_pipeline.qupath_runtime.runner import format_qupath_failure_record
            diagnostic += "\n\n" + format_qupath_failure_record(error.record)
        except Exception as diagnostic_error:
            diagnostic += "\n\nSaved QuPath log details could not be read: " + str(diagnostic_error)
    return diagnostic


def _failure_outcome(
    job_id: str,
    operation: str,
    error: BaseException,
) -> DesktopJobError:
    """

    Creates a failure result that can still be delivered when exception formatting fails.

    We obtain the exception name, message and traceback separately so failure
    in one formatting step does not discard information from the other steps.
    A final fallback constructs a plain error result if this reporting sequence
    raises unexpectedly. The result can then enter the same queue as a successful
    operation and release its active-job entry on the Qt thread.

    Args:
        job_id: Identifier retained for the failed operation.
        operation: Name supplied when it was started.
        error: Original exception raised in the worker thread.

    Returns:
        DesktopJobError containing available details or the fixed fallback text.
    """

    try:
        error_type = _error_type_name(error)
        message = _error_message(error, error_type)
        diagnostic = _error_diagnostic(error, error_type, message)
        return DesktopJobError(
            job_id,
            operation,
            error_type,
            message,
            diagnostic,
        )
    except BaseException:
        return DesktopJobError(
            job_id,
            operation,
            _FALLBACK_ERROR_TYPE,
            _FALLBACK_ERROR_MESSAGE,
            f"{_FALLBACK_ERROR_TYPE}: {_FALLBACK_ERROR_MESSAGE}",
        )


@dataclass(slots=True)
class _ActiveJob:
    """

    Retains the thread and identifiers needed until one operation has fully ended.

    Attributes:
        job_id: Unique identifier used by signals and the result queue.
        operation: Displayed name of the running operation.
        access: READ, MUTATION, RUN or CONTROL dispatch category.
        thread: Non-daemon Python thread executing the supplied callable.
        coalesce_key: Optional read-request key used to reuse this operation.
        run_job_id: Active run targeted by a pause/cancel call; absent for
            ordinary read, write and run operations.
    """

    job_id: str
    operation: str
    access: JobAccess
    thread: Thread
    coalesce_key: str | None
    run_job_id: str | None


class ManagedJobBridge(QObject):
    """

    Coordinates background operations and delivers their completed results on the Qt thread.

    We retain active threads by job ID until they have ended and their queued
    result can be delivered. This lets the interface display current operations
    and prevents a second settings-changing operation from starting during the
    previous worker's final cleanup.

    The bridge uses a queue for communication from Python workers. Its Qt timer
    runs only while an operation or undelivered result remains. Worker code
    returns data; the success/failure signals are emitted later by Qt so the
    receiving form can change its widgets on the application thread.

    Attributes:
        job_succeeded: Signal carrying the completed job ID and returned value.
        job_failed: Signal carrying the failed job ID and DesktopJobError.
        active_jobs_changed: Signal carrying the current tuple of active IDs
            after a job is added or removed.
    """

    job_succeeded = Signal(str, object)
    job_failed = Signal(str, object)
    active_jobs_changed = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        """

        Initializes the result queue, active-job lists and Qt completion timer.

        The bridge must be constructed on the existing Qt application thread.
        Its 25-millisecond timer starts when the first job is submitted and stops
        once no active thread or pending result remains.

        Args:
            parent: Optional Qt parent for this bridge and its timer.

        Raises:
            RuntimeError: No Qt application exists or construction occurs on a
                different thread.
        """

        super().__init__(parent)
        application = QCoreApplication.instance()
        if application is None or self.thread() is not application.thread():
            raise RuntimeError("ManagedJobBridge must be created on the application thread")
        self._outcomes: SimpleQueue[_JobOutcome] = SimpleQueue()
        self._pending_outcomes: dict[str, _JobOutcome] = {}
        self._active: dict[str, _ActiveJob] = {}
        self._coalesced_reads: dict[str, str] = {}
        self._mutation_guard: Callable[[], None] | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._drain_outcomes)

    @property
    def active_job_ids(self) -> tuple[str, ...]:
        """

        Returns the identifiers of operations still awaiting completed delivery.

        Returns:
            Tuple of active job IDs in submission order. A worker that has just
            ended remains listed until its queued result is processed by Qt.

        Raises:
            RuntimeError: The property is read outside the Qt application thread.
        """

        self._require_application_thread()
        return tuple(self._active)

    @property
    def active_operations(self) -> tuple[str, ...]:
        """

        Returns the operation labels used to explain active work in the interface.

        Returns:
            Tuple of operation names corresponding to the current active jobs.
            Names can repeat when separate operations share the same displayed label.

        Raises:
            RuntimeError: The property is read outside the Qt application thread.
        """

        self._require_application_thread()
        return tuple(job.operation for job in self._active.values())

    @property
    def has_active_jobs(self) -> bool:
        """

        Checks whether any operation still needs to finish or deliver its result.

        Returns:
            True while the active-job dictionary contains an entry. The result can
            remain True briefly after a worker exits, until Qt joins and removes it.

        Raises:
            RuntimeError: The property is read outside the Qt application thread.
        """

        self._require_application_thread()
        return bool(self._active)

    @property
    def active_run_job_id(self) -> str | None:
        """

        Finds the analysis run currently retained by this bridge.

        Returns:
            RUN job ID, or None when no run is active. submit_control() additionally
            checks that its thread is still alive before sending pause or cancel.

        Raises:
            RuntimeError: The property is read outside the Qt application thread.
        """

        self._require_application_thread()
        return next(
            (job.job_id for job in self._active.values() if job.access is JobAccess.RUN),
            None,
        )

    def submit(
        self,
        operation: str,
        callable_: Callable[[], object],
        *,
        access: JobAccess,
        coalesce_key: str | None = None,
    ) -> str:
        """

        Starts a checked operation or reuses an equivalent read already in progress.

        For READ with a coalesce_key, an active read with that same key is returned
        immediately without executing the newly supplied callable. The caller must
        therefore construct the key from the actual request being reused; this
        function does not compare captured callable arguments.

        For MUTATION and RUN, another write, run or control call blocks submission.
        When a mutation guard is installed, it is checked on the Qt thread before
        the new worker starts. CONTROL requests use submit_control() because they
        must identify the specific running analysis they intend to pause or cancel.

        Args:
            operation: Nonempty displayed name for the operation.
            callable_: Zero-argument function performing the read, fit or analysis work.
            access: Exact JobAccess category describing the operation.
            coalesce_key: Optional nonempty key identifying an equivalent READ request.

        Returns:
            New job ID, or the active read's existing ID when its key matches.

        Raises:
            ValueError: A name/key is invalid or CONTROL is submitted through this method.
            TypeError: The supplied operation is not callable or access is not JobAccess.
            RuntimeError: A conflicting write/run/control is active or the call is
                made outside the application thread. The installed guard can also
                raise when the analysis is not ready for a new write.
        """

        self._require_application_thread()
        self._validate_submission(operation, callable_, access, coalesce_key)
        if access is JobAccess.CONTROL:
            raise ValueError("control jobs require submit_control")
        # Reuse a read only under the explicit request key supplied by its
        # caller; the new callable is not started when that key is active.
        if access is JobAccess.READ and coalesce_key is not None:
            existing = self._coalesced_reads.get(coalesce_key)
            if existing is not None:
                return existing
        if access in {JobAccess.MUTATION, JobAccess.RUN} and any(
            job.access in {JobAccess.MUTATION, JobAccess.RUN, JobAccess.CONTROL}
            for job in self._active.values()
        ):
            raise RuntimeError("another run or mutation job is active")
        if access in {JobAccess.MUTATION, JobAccess.RUN} and self._mutation_guard is not None:
            self._mutation_guard()
        return self._start_job(
            operation,
            callable_,
            access=access,
            coalesce_key=coalesce_key,
            run_job_id=None,
        )

    def bind_mutation_guard(self, guard: Callable[[], None]) -> None:
        """

        Sets the application-thread check used before new settings-changing operations.

        The supplied function can check whether the current analysis or project
        permits a write without beginning that work in a background thread. It is
        called by submit() for MUTATION and RUN, after the concurrency check.

        Args:
            guard: Fast zero-argument function that returns normally when submission
                is permitted or raises an error explaining why it is blocked.

        Raises:
            TypeError: guard is not callable.
            RuntimeError: A guard was already set or the call is outside the Qt thread.
        """

        self._require_application_thread()
        if not callable(guard):
            raise TypeError("mutation guard must be callable")
        if self._mutation_guard is not None:
            raise RuntimeError("mutation guard is already bound")
        self._mutation_guard = guard

    def submit_control(
        self,
        run_job_id: str,
        operation: Literal["pause", "cancel"],
        callable_: Callable[[], object],
    ) -> str:
        """

        Sends a pause or cancel request to the exact analysis run still executing.

        We require the supplied run_job_id to identify a live RUN thread and allow
        only one CONTROL operation at a time. The control callable runs on its own
        thread so requesting a pause does not block the interface while the analysis
        reaches a safe stopping point.

        Args:
            run_job_id: ID of the live analysis run to control.
            operation: "pause" or "cancel".
            callable_: Zero-argument function that requests the chosen operation
                from that run. The analysis code implements stopping and cleanup.

        Returns:
            ID of the newly started control job.

        Raises:
            ValueError: The run is absent, already ended or not a RUN job, or the
                requested control operation is unsupported.
            TypeError: callable_ is not callable.
            RuntimeError: Another control request is active or the call is outside Qt.
        """

        self._require_application_thread()
        if type(run_job_id) is not str or not run_job_id.strip():
            raise ValueError("control requires an exact active run job ID")
        if operation not in {"pause", "cancel"}:
            raise ValueError("control operation must be pause or cancel")
        if not callable(callable_):
            raise TypeError("job callable must be callable")
        run = self._active.get(run_job_id)
        if run is None or run.access is not JobAccess.RUN or not run.thread.is_alive():
            raise ValueError("control requires the exact active run job")
        if any(job.access is JobAccess.CONTROL for job in self._active.values()):
            raise RuntimeError("a control job is already active")
        return self._start_job(
            operation,
            callable_,
            access=JobAccess.CONTROL,
            coalesce_key=None,
            run_job_id=run_job_id,
        )

    def _validate_submission(
        self,
        operation: object,
        callable_: object,
        access: object,
        coalesce_key: object,
    ) -> None:
        """

        Checks the names and types used to start an ordinary background operation.

        A reuse key is permitted only for READ because repeating a write or fit can
        require a separate operation even when its displayed name is unchanged.
        This validation occurs before a thread or active-job entry is created.

        Args:
            operation: Displayed operation name, required to be nonempty text.
            callable_: Function to execute without arguments.
            access: Exact JobAccess value used for concurrency decisions.
            coalesce_key: Optional nonempty read-request key.

        Raises:
            ValueError: An operation/key is empty or a non-read has a reuse key.
            TypeError: The callable or access value has an incompatible type.
        """

        if type(operation) is not str or not operation.strip():
            raise ValueError("job operation must be non-empty text")
        if not callable(callable_):
            raise TypeError("job callable must be callable")
        if type(access) is not JobAccess:
            raise TypeError("job access must be a JobAccess value")
        if coalesce_key is not None and (type(coalesce_key) is not str or not coalesce_key.strip()):
            raise ValueError("coalesce key must be non-empty text")
        if access is not JobAccess.READ and coalesce_key is not None:
            raise ValueError("only read jobs may have a coalesce key")

    def _start_job(
        self,
        operation: str,
        callable_: Callable[[], object],
        *,
        access: JobAccess,
        coalesce_key: str | None,
        run_job_id: str | None,
    ) -> str:
        """

        Records a new operation, starts its Python thread and arranges result delivery through Qt.

        We register the thread and optional read key before starting execution,
        then emit the updated active-job list and start the completion timer if
        needed. If thread startup fails, those entries are removed and the active
        list is emitted again before the startup error propagates.

        The worker catches an operation failure and places either its returned data
        or formatted error on the queue. It does not emit widget-update signals
        itself. _drain_outcomes() later joins the finished worker and delivers its
        result from the application thread.

        Args:
            operation: Validated displayed operation name.
            callable_: Zero-argument function to execute in the worker.
            access: Validated dispatch category.
            coalesce_key: Optional key for this READ operation.
            run_job_id: Target RUN identifier for a pause/cancel call, otherwise None.

        Returns:
            Identifier assigned to the newly started thread.

        Raises:
            RuntimeError: The Python thread cannot be started. Other startup exceptions
                also propagate after removing the incomplete active-job entry.
        """

        job_id = uuid4().hex

        def run_operation() -> None:
            """

            Executes the submitted function and places one completed outcome on the queue.

            Successful results must be data rather than QObjects, since Qt objects
            need to be created and accessed on their designated Qt thread. An operation
            error, including an error during exception formatting, is converted to a
            DesktopJobError so Qt can still finish the job and release its active entry.

            The callable, displayed operation and job ID are retained from _start_job().
            After this function returns, the timer's next pass can join the ended thread
            and emit the outcome on the application thread.
            """

            try:
                try:
                    value = callable_()
                    if isinstance(value, QObject):
                        raise TypeError("worker operation returned a QObject")
                    outcome: _JobOutcome = DesktopJobSuccess(
                        job_id,
                        operation,
                        value,
                    )
                except BaseException as exc:
                    outcome = _failure_outcome(job_id, operation, exc)
            except BaseException:
                outcome = DesktopJobError(
                    job_id,
                    operation,
                    _FALLBACK_ERROR_TYPE,
                    _FALLBACK_ERROR_MESSAGE,
                    f"{_FALLBACK_ERROR_TYPE}: {_FALLBACK_ERROR_MESSAGE}",
                )
            self._outcomes.put(outcome)

        thread = Thread(
            target=run_operation,
            name=f"mif-desktop-{job_id}",
            daemon=False,
        )
        # Register the operation before thread.start(), since a short read
        # can finish and queue its result immediately after the thread begins.
        active = _ActiveJob(
            job_id,
            operation,
            access,
            thread,
            coalesce_key,
            run_job_id,
        )
        self._active[job_id] = active
        if coalesce_key is not None:
            self._coalesced_reads[coalesce_key] = job_id
        self.active_jobs_changed.emit(self.active_job_ids)
        if not self._timer.isActive():
            self._timer.start()
        try:
            thread.start()
        except BaseException:
            self._remove_active(active)
            if not self._active and not self._pending_outcomes:
                self._timer.stop()
            self.active_jobs_changed.emit(self.active_job_ids)
            raise
        return job_id

    def _drain_outcomes(self) -> None:
        """

        Delivers queued results only after their Python workers have fully ended.

        First move every currently available queue entry into the pending-result
        dictionary. For each pending result, check its thread. An alive thread is
        retained for the next timer pass; an ended thread is joined and removed
        from both the active and pending lists.

        We then emit the updated active IDs followed by the job's success or failure
        signal. Removing the ended job before those signals allows a completion
        handler to submit the next operation without overlapping the previous worker.
        The timer stops when neither active work nor pending results remain.

        Raises:
            RuntimeError: Completion is processed outside the Qt application thread.
        """

        self._require_application_thread()
        while True:
            try:
                outcome = self._outcomes.get_nowait()
            except Empty:
                break
            self._pending_outcomes[outcome.job_id] = outcome

        ready: list[_JobOutcome] = []
        for job_id, outcome in tuple(self._pending_outcomes.items()):
            active = self._active.get(job_id)
            if active is None or active.thread.is_alive():
                continue
            # Join only an ended thread, then release its write/read slot
            # before a completion handler can submit the next operation.
            active.thread.join()
            self._remove_active(active)
            del self._pending_outcomes[job_id]
            ready.append(outcome)

        for outcome in ready:
            self.active_jobs_changed.emit(self.active_job_ids)
            if isinstance(outcome, DesktopJobSuccess):
                self.job_succeeded.emit(outcome.job_id, outcome.value)
            else:
                self.job_failed.emit(outcome.job_id, outcome)

        if not self._active and not self._pending_outcomes:
            self._timer.stop()

    def _remove_active(self, active: _ActiveJob) -> None:
        """

        Removes one ended or unstarted job from the active lists.

        Its read key is removed only when the key still points to that exact job.
        This avoids deleting a newer read's mapping if the key has already been
        associated with another operation.

        Args:
            active: Job being removed after failed startup or completed thread joining.
        """

        self._active.pop(active.job_id, None)
        key = active.coalesce_key
        if key is not None and self._coalesced_reads.get(key) == active.job_id:
            del self._coalesced_reads[key]

    def _require_application_thread(self) -> None:
        """

        Requires active-job state and Qt signals to be accessed on the application thread.

        Worker functions communicate through the thread-safe queue. The dictionaries,
        timer and Qt signals are instead read and changed by the application's thread.

        Raises:
            RuntimeError: No Qt application exists or the current thread differs
                from its application thread.
        """

        application = QCoreApplication.instance()
        if application is None or QThread.currentThread() is not application.thread():
            raise RuntimeError("managed jobs may be accessed only on the application thread")


__all__ = [
    "DesktopJobError",
    "DesktopJobSuccess",
    "JobAccess",
    "ManagedJobBridge",
]
