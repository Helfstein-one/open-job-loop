from __future__ import annotations
"""
Execution harness subsystem for open-job-loop.

Provides:
- CircuitState: Enum for MCP circuit breaker states (CLOSED, OPEN, HALF_OPEN).
- MCPCircuitBreaker: Resilient state machine preventing cascading failures to MCP servers.
- LocalLoopGuard: Execution harness enforcing iteration limits and wall-clock timeouts,
  catching TimeoutError / LLMTimeoutError, logging, marking JobStatus.SKIPPED_TIMEOUT,
  and gracefully skipping without crashing.
- HarnessError, CircuitOpenError, MaxIterationsReachedError: Specialized exceptions.
"""


import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, TypeVar

from src.infrastructure.adapters.llm_evaluator import LLMTimeoutError
from src.domain.models import JobPosting, JobStatus

if TYPE_CHECKING:
    from src.infrastructure.adapters.repository import JobRepository

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ==============================================================================
# Exceptions
# ==============================================================================

class HarnessError(Exception):
    """Base exception for execution harness errors."""


class CircuitOpenError(HarnessError):
    """Raised when an operation is blocked because the circuit breaker is OPEN."""


class MaxIterationsReachedError(HarnessError):
    """Raised when the execution loop exceeds the configured max_iterations limit."""


# ==============================================================================
# MCP Circuit Breaker
# ==============================================================================

class CircuitState(str, Enum):
    """
    States for the MCP circuit breaker state machine.
    """
    CLOSED = "CLOSED"        # Normal operation: requests allowed
    OPEN = "OPEN"            # Tripped: requests blocked
    HALF_OPEN = "HALF_OPEN"  # Probe state: trial request allowed to verify recovery


class MCPCircuitBreaker:
    """
    Circuit breaker for Model Context Protocol (MCP) server interactions.

    State transitions:
    - CLOSED -> OPEN: Triggered when consecutive failures reach `failure_threshold`.
    - OPEN -> HALF_OPEN: Triggered when `recovery_time` seconds elapse since the last failure.
    - HALF_OPEN -> CLOSED: Triggered on a successful request; resets failure count to 0.
    - HALF_OPEN -> OPEN: Triggered on any failure during probe; resets recovery timer.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_time: float = 30.0,
        time_provider: Callable[[], float] | None = None,
    ) -> None:
        """
        Initialize the MCP circuit breaker.

        Args:
            failure_threshold: Number of consecutive failures before opening the circuit.
            recovery_time: Seconds to wait in OPEN state before transitioning to HALF_OPEN.
            time_provider: Monotonic clock provider function (defaults to time.monotonic).
        """
        if failure_threshold < 1:
            raise ValueError(f"failure_threshold must be >= 1, got {failure_threshold}")
        if recovery_time <= 0:
            raise ValueError(f"recovery_time must be > 0, got {recovery_time}")

        self._failure_threshold = failure_threshold
        self._recovery_time = recovery_time
        self._time_provider = time_provider or time.monotonic

        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._last_failure_time: float | None = None
        self._total_trips: int = 0

    @property
    def state(self) -> CircuitState:
        """
        Current state of the circuit breaker.
        Automatically evaluates time-based transition from OPEN to HALF_OPEN.
        """
        if self._state == CircuitState.OPEN and self._last_failure_time is not None:
            now = self._time_provider()
            if now - self._last_failure_time >= self._recovery_time:
                logger.info(
                    "MCPCircuitBreaker recovery_time (%.1fs) elapsed. Transitioning OPEN -> HALF_OPEN.",
                    self._recovery_time,
                )
                self._state = CircuitState.HALF_OPEN
        return self._state

    @property
    def failure_count(self) -> int:
        """Current consecutive failure count."""
        return self._failure_count

    @property
    def failure_threshold(self) -> int:
        """Configured failure threshold."""
        return self._failure_threshold

    @property
    def recovery_time(self) -> float:
        """Configured recovery timeout in seconds."""
        return self._recovery_time

    @property
    def last_failure_time(self) -> float | None:
        """Timestamp of the most recent failure, or None."""
        return self._last_failure_time

    @property
    def total_trips(self) -> int:
        """Total number of times the circuit tripped to OPEN."""
        return self._total_trips

    def allow_request(self) -> bool:
        """
        Check whether a request is permitted under current circuit state.

        Returns:
            True if state is CLOSED or HALF_OPEN (trial probe).
            False if state is OPEN.
        """
        current = self.state
        return current in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def record_success(self) -> None:
        """
        Record a successful request execution.
        Resets failure count and transitions HALF_OPEN back to CLOSED.
        """
        prev_state = self.state
        if prev_state == CircuitState.HALF_OPEN:
            logger.info("MCPCircuitBreaker probe succeeded. Transitioning HALF_OPEN -> CLOSED.")
            self._state = CircuitState.CLOSED
        self._failure_count = 0

    def record_failure(self) -> None:
        """
        Record a failed request execution.
        Increments failure count and trips to OPEN if threshold reached or if in HALF_OPEN.
        """
        now = self._time_provider()
        self._last_failure_time = now
        prev_state = self.state

        if prev_state == CircuitState.HALF_OPEN:
            logger.warning(
                "MCPCircuitBreaker probe failed during HALF_OPEN. Re-tripping to OPEN."
            )
            self._state = CircuitState.OPEN
            self._total_trips += 1
            return

        self._failure_count += 1
        if self._failure_count >= self._failure_threshold:
            logger.warning(
                "MCPCircuitBreaker failure threshold (%d) reached. Transitioning CLOSED -> OPEN.",
                self._failure_threshold,
            )
            self._state = CircuitState.OPEN
            self._total_trips += 1

    def reset(self) -> None:
        """
        Manually reset the circuit breaker to CLOSED state with 0 failures.
        """
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = None

    async def call(
        self,
        func: Callable[..., Awaitable[T]] | Awaitable[T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """
        Execute an async callable protected by the circuit breaker.

        Args:
            func: Async callable or coroutine to execute.
            *args: Positional arguments for func.
            **kwargs: Keyword arguments for func.

        Returns:
            Result of func.

        Raises:
            CircuitOpenError: If allow_request() is False.
            Exception: Any exception raised by func (recorded as a failure).
        """
        if not self.allow_request():
            raise CircuitOpenError(
                f"MCPCircuitBreaker is {self.state.value}. Request blocked."
            )

        try:
            if inspect.iscoroutine(func):
                result = await func
            elif callable(func):
                call_res = func(*args, **kwargs)
                if inspect.iscoroutine(call_res):
                    result = await call_res
                else:
                    result = call_res
            else:
                raise TypeError(f"Expected callable or coroutine, got {type(func).__name__}")
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise

    async def __aenter__(self) -> Self:
        """Async context manager entry: check circuit permissions."""
        if not self.allow_request():
            raise CircuitOpenError(
                f"MCPCircuitBreaker is {self.state.value}. Request blocked."
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        """Async context manager exit: record success or failure."""
        if exc_val is not None:
            self.record_failure()
        else:
            self.record_success()
        return False


# ==============================================================================
# Local Loop Guard
# ==============================================================================

class LocalLoopGuard:
    """
    Execution harness guarding closed loop pipeline execution.

    Responsibilities:
    1. Bounded iteration protection: tracks and enforces `max_iterations`.
    2. Wall-clock timeout enforcement: wraps operations with `asyncio.timeout(timeout_seconds)`.
    3. Graceful degradation: catches `TimeoutError` and `LLMTimeoutError`, logs warnings,
       marks target `JobPosting.status = JobStatus.SKIPPED_TIMEOUT`, flushes immediately
       to DuckDB repository (if provided), and cleanly returns None without crashing.
    4. Optional circuit breaker integration for MCP server upstream health protection.
    """

    def __init__(
        self,
        max_iterations: int = 50,
        timeout_seconds: float = 15.0,
        circuit_breaker: MCPCircuitBreaker | None = None,
        repository: JobRepository | None = None,
    ) -> None:
        """
        Initialize LocalLoopGuard.

        Args:
            max_iterations: Maximum loop iterations permitted before halting.
            timeout_seconds: Per-step or per-job wall-clock timeout in seconds.
            circuit_breaker: Optional MCPCircuitBreaker instance.
            repository: Optional JobRepository for immediate status persistence on timeout.
        """
        if max_iterations < 1:
            raise ValueError(f"max_iterations must be >= 1, got {max_iterations}")
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be > 0, got {timeout_seconds}")

        self.max_iterations = max_iterations
        self.timeout_seconds = timeout_seconds
        self.circuit_breaker = circuit_breaker
        self.repository = repository

        self.current_iteration: int = 0
        self.total_completed: int = 0
        self.total_timeouts: int = 0
        self.total_skipped: int = 0
        self.total_errors: int = 0

    def can_continue(self) -> bool:
        """
        Check if the execution loop has remaining iterations.

        Returns:
            True if current_iteration < max_iterations, False otherwise.
        """
        return self.current_iteration < self.max_iterations

    def check_iteration_limit(self) -> None:
        """
        Raise MaxIterationsReachedError if the iteration limit has been reached or exceeded.
        """
        if self.current_iteration >= self.max_iterations:
            raise MaxIterationsReachedError(
                f"Iteration limit reached: {self.current_iteration}/{self.max_iterations} iterations."
            )

    def increment_iteration(self) -> int:
        """
        Increment and return current iteration counter.
        """
        self.current_iteration += 1
        return self.current_iteration

    def reset(self) -> None:
        """
        Reset iteration counter and telemetry metrics to initial state.
        """
        self.current_iteration = 0
        self.total_completed = 0
        self.total_timeouts = 0
        self.total_skipped = 0
        self.total_errors = 0

    def _extract_job(
        self,
        explicit_job: JobPosting | None,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> JobPosting | None:
        """
        Extract JobPosting entity from explicit argument, kwargs, or args tuple.
        """
        if explicit_job is not None:
            return explicit_job
        if "job" in kwargs and isinstance(kwargs["job"], JobPosting):
            return kwargs["job"]
        for arg in args:
            if isinstance(arg, JobPosting):
                return arg
        return None

    async def run_guarded(
        self,
        step_func: Callable[..., Awaitable[T]] | Awaitable[T],
        *args: Any,
        job: JobPosting | None = None,
        step_name: str | None = None,
        repository: JobRepository | None = None,
        **kwargs: Any,
    ) -> T | None:
        """
        Execute a coroutine wrapped in wall-clock timeout and iteration guards.

        If execution exceeds `timeout_seconds`, catches TimeoutError / LLMTimeoutError:
        - Logs warning with duration and step details.
        - Sets job.status = JobStatus.SKIPPED_TIMEOUT and populates job.error_message.
        - Flushes status update to database repository (if provided).
        - Increments telemetry counters.
        - Returns None gracefully without crashing the loop.

        Args:
            step_func: Async callable or coroutine to execute.
            *args: Positional arguments passed to step_func.
            job: Target JobPosting domain entity (optional, auto-detected if in args/kwargs).
            step_name: Descriptive name for logging / telemetry (defaults to func.__name__).
            repository: Repository override for immediate flush (defaults to self.repository).
            **kwargs: Keyword arguments passed to step_func.

        Returns:
            The return value of step_func on success, or None if skipped due to timeout.

        Raises:
            MaxIterationsReachedError: If current_iteration >= max_iterations.
            CircuitOpenError: If circuit_breaker is OPEN and blocks request.
            Exception: Any non-timeout exception raised by step_func is re-raised.
        """
        self.check_iteration_limit()
        self.current_iteration += 1

        target_job = self._extract_job(job, args, kwargs)
        target_repo = repository or self.repository
        name = step_name or getattr(step_func, "__name__", "guarded_step")

        # Optional circuit breaker check
        if self.circuit_breaker is not None and not self.circuit_breaker.allow_request():
            logger.warning(
                "LocalLoopGuard step '%s' aborted: MCPCircuitBreaker is %s.",
                name,
                self.circuit_breaker.state.value,
            )
            if target_job is not None:
                target_job.status = JobStatus.ERROR
                target_job.error_message = (
                    f"Circuit breaker is {self.circuit_breaker.state.value}"
                )
                if target_repo is not None:
                    try:
                        await target_repo.update_status(
                            job_id=target_job.id,
                            status=JobStatus.ERROR,
                            error_message=target_job.error_message,
                        )
                    except Exception as db_err:  # noqa: BLE001
                        logger.error("Failed to update job status on circuit open: %s", db_err)
            raise CircuitOpenError(
                f"MCPCircuitBreaker is {self.circuit_breaker.state.value}. Step '{name}' blocked."
            )

        # Prepare kwargs to pass to step_func cleanly
        call_kwargs = dict(kwargs)
        if callable(step_func):
            try:
                sig = inspect.signature(step_func)
                bound = sig.bind_partial(*args)
                if "job" in call_kwargs:
                    if "job" in bound.arguments or "job" not in sig.parameters:
                        del call_kwargs["job"]
                elif job is not None and "job" in sig.parameters and "job" not in bound.arguments:
                    call_kwargs["job"] = job
            except (ValueError, TypeError):
                pass

        try:
            async with asyncio.timeout(self.timeout_seconds):
                if inspect.iscoroutine(step_func):
                    result = await step_func
                elif callable(step_func):
                    call_res = step_func(*args, **call_kwargs)
                    if inspect.iscoroutine(call_res):
                        result = await call_res
                    else:
                        result = call_res
                else:
                    raise TypeError(
                        f"step_func must be callable or coroutine, got {type(step_func).__name__}"
                    )

            if self.circuit_breaker is not None:
                self.circuit_breaker.record_success()

            self.total_completed += 1
            return result

        except (TimeoutError, LLMTimeoutError) as exc:
            self.total_timeouts += 1
            self.total_skipped += 1
            logger.warning(
                "LocalLoopGuard timeout (%.1fs limit) during step '%s': %s. Gracefully skipping.",
                self.timeout_seconds,
                name,
                exc,
            )

            if target_job is not None:
                target_job.status = JobStatus.SKIPPED_TIMEOUT
                target_job.error_message = (
                    f"Execution timed out after {self.timeout_seconds}s in step '{name}': {exc}"
                )

                if target_repo is not None:
                    try:
                        await target_repo.update_status(
                            job_id=target_job.id,
                            status=JobStatus.SKIPPED_TIMEOUT,
                            error_message=target_job.error_message,
                        )
                        logger.info(
                            "Updated job %s status to SKIPPED_TIMEOUT in repository.",
                            target_job.id,
                        )
                    except Exception as db_err:  # noqa: BLE001
                        logger.error(
                            "Failed to persist SKIPPED_TIMEOUT for job %s: %s",
                            target_job.id,
                            db_err,
                        )

            return None

        except Exception as exc:
            self.total_errors += 1
            if self.circuit_breaker is not None:
                self.circuit_breaker.record_failure()
            logger.error("LocalLoopGuard caught unhandled error in step '%s': %s", name, exc)
            raise
