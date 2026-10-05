# M3 Explorer 1: Execution Harness Technical Specification & Code Design

## Executive Summary
This report provides the complete, production-ready architecture, interface contracts, and implementation design for the execution harness subsystem of **open-job-loop**:
1. `src/core/harness.py`:
   - **`MCPCircuitBreaker`**: A state machine (`CLOSED`, `OPEN`, `HALF_OPEN`) protecting against cascading failures during MCP ingestion. Configurable `failure_threshold` (consecutive failures) and `recovery_time` (seconds before trial probe). Resets to `CLOSED` upon successful probe; re-trips to `OPEN` on failure. Provides `allow_request()`, `record_success()`, `record_failure()`, `reset()`, async `call()`, and async context manager (`async with breaker:`).
   - **`LocalLoopGuard`**: Wall-clock execution guard enforcing `max_iterations` and `timeout_seconds`. Coroutines wrapped via `asyncio.timeout()`; catches `TimeoutError` and `LLMTimeoutError`, logs warnings, marks `job.status = JobStatus.SKIPPED_TIMEOUT`, populates `job.error_message`, triggers immediate status persistence in `JobRepository` (if present), and cleanly skips without crashing.
   - **Exceptions**: `HarnessError`, `CircuitOpenError`, `MaxIterationsReachedError`.
2. `tests/test_harness.py`:
   - Full 25-case unit test suite testing enum values, state machine transitions, wall-clock timeout wrapping, LLM timeout handling, repository persistence, error propagation, and multi-job DAG batch loops.

---

## 1. Architectural Design & Component Interactions

```
                       ┌────────────────────────────┐
                       │   DAG Pipeline / Loop      │
                       └─────────────┬──────────────┘
                                     │
           ┌─────────────────────────▼─────────────────────────┐
           │                  LocalLoopGuard                   │
           │  - max_iterations (loop guard)                    │
           │  - timeout_seconds (asyncio.timeout wall-clock)   │
           │  - catches TimeoutError & LLMTimeoutError         │
           │  - updates JobStatus.SKIPPED_TIMEOUT              │
           └─────────────┬─────────────────────────┬───────────┘
                         │ wraps step              │ delegates MCP
                         ▼                         ▼
            ┌────────────────────────┐  ┌─────────────────────────┐
            │   Pipeline Step Func   │  │    MCPCircuitBreaker    │
            │   (Triage / Evaluator) │  │  - CLOSED / OPEN /      │
            └────────────┬───────────┘  │    HALF_OPEN            │
                         │              │  - failure_threshold    │
                         ▼              │  - recovery_time        │
            ┌────────────────────────┐  └──────────┬──────────────┘
            │ DuckDB / JobRepository │             │ protects
            │ (Immediate flush on    │             ▼
            │  SKIPPED_TIMEOUT)      │  ┌─────────────────────────┐
            └────────────────────────┘  │     McpJobClient        │
                                        └─────────────────────────┘
```

### State Machine for `MCPCircuitBreaker`
```
             [Success / Reset]
           ┌───────────────────┐
           │                   │
           ▼                   │
    ┌─────────────┐     consecutive failures     ┌────────────┐
    │   CLOSED    │ ───────────────────────────> │    OPEN    │
    │ (Allow all) │    >= failure_threshold      │(Block all) │
    └─────────────┘                              └─────┬──────┘
           ▲                                           │
           │ probe success                             │ elapsed >= recovery_time
           │                                           ▼
           └─────────────────────────────────────┌────────────┐
                                                 │ HALF_OPEN  │
                probe failure                    │(Trial probe│
                ────────────────────────────────>│  allowed)  │
                                                 └────────────┘
```

---

## 2. Complete Code Specification for `src/core/harness.py`

```python
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

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, TypeVar

from src.llm.evaluator import LLMTimeoutError
from src.models.schemas import JobPosting, JobStatus

if TYPE_CHECKING:
    from src.db.repository import JobRepository

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
        time_provider: Optional[Callable[[], float]] = None,
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
        self._last_failure_time: Optional[float] = None
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
    def last_failure_time(self) -> Optional[float]:
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
        current = self.state  # Triggers time-based transition if applicable
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

    async def __aenter__(self) -> MCPCircuitBreaker:
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
        exc_tb: Any,
    ) -> bool:
        """Async context manager exit: record success or failure."""
        if exc_val is not None:
            self.record_failure()
        else:
            self.record_success()
        return False  # Do not suppress exceptions


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
        circuit_breaker: Optional[MCPCircuitBreaker] = None,
        repository: Optional[JobRepository] = None,
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
        explicit_job: Optional[JobPosting],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Optional[JobPosting]:
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
        job: Optional[JobPosting] = None,
        step_name: Optional[str] = None,
        repository: Optional[JobRepository] = None,
        **kwargs: Any,
    ) -> Optional[T]:
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
                    except Exception as db_err:
                        logger.error("Failed to update job status on circuit open: %s", db_err)
            raise CircuitOpenError(
                f"MCPCircuitBreaker is {self.circuit_breaker.state.value}. Step '{name}' blocked."
            )

        # Prepare kwargs to pass to step_func
        call_kwargs = dict(kwargs)
        if job is not None and callable(step_func):
            try:
                sig = inspect.signature(step_func)
                if "job" in sig.parameters and "job" not in call_kwargs:
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
                    except Exception as db_err:
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
```

---

## 3. Complete Test Suite Specification for `tests/test_harness.py`

```python
"""
Unit tests for execution harness (MCPCircuitBreaker and LocalLoopGuard).

Tests:
1. CircuitState enumeration values.
2. MCPCircuitBreaker state machine:
   - Initial CLOSED state.
   - Failure threshold tripping to OPEN.
   - Request blocking in OPEN state.
   - Recovery time transition to HALF_OPEN.
   - Probe success recovery to CLOSED.
   - Probe failure re-tripping to OPEN.
   - Manual reset functionality.
   - call() execution and async context manager.
3. LocalLoopGuard:
   - Configuration defaults and validation.
   - Iteration bounding and MaxIterationsReachedError.
   - Wall-clock timeout enforcement via asyncio.timeout.
   - Catching TimeoutError and LLMTimeoutError.
   - Transitioning JobPosting status to SKIPPED_TIMEOUT with error message.
   - Immediate repository status updates on timeout.
   - Graceful skip without crashing.
   - Exception propagation for non-timeout errors.
   - Combined circuit breaker and loop guard workflows.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.harness import (
    CircuitOpenError,
    CircuitState,
    HarnessError,
    LocalLoopGuard,
    MaxIterationsReachedError,
    MCPCircuitBreaker,
)
from src.llm.evaluator import LLMError, LLMTimeoutError
from src.models.schemas import JobPosting, JobStatus


# ==============================================================================
# Fixtures & Helpers
# ==============================================================================

class MockClock:
    """Controllable monotonic clock for deterministic time simulation."""

    def __init__(self, start_time: float = 1000.0) -> None:
        self.current_time = start_time

    def __call__(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


@pytest.fixture
def mock_clock() -> MockClock:
    return MockClock()


@pytest.fixture
def sample_job() -> JobPosting:
    return JobPosting(
        id="job-test-123",
        content_hash="a" * 64,
        title="Distributed Systems Engineer",
        company="TechCorp",
        raw_description="Build distributed job processing pipelines in Python.",
        status=JobStatus.INGESTED,
    )


@pytest.fixture
def mock_repository() -> MagicMock:
    repo = MagicMock()
    repo.update_status = AsyncMock(return_value=True)
    return repo


# ==============================================================================
# 1. CircuitState Enum Tests
# ==============================================================================

class TestCircuitState:
    def test_enum_members(self):
        assert CircuitState.CLOSED == "CLOSED"
        assert CircuitState.OPEN == "OPEN"
        assert CircuitState.HALF_OPEN == "HALF_OPEN"
        assert len(CircuitState) == 3


# ==============================================================================
# 2. MCPCircuitBreaker Unit Tests
# ==============================================================================

class TestMCPCircuitBreaker:
    def test_initial_state(self, mock_clock):
        cb = MCPCircuitBreaker(failure_threshold=3, recovery_time=10.0, time_provider=mock_clock)
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.failure_threshold == 3
        assert cb.recovery_time == 10.0
        assert cb.allow_request() is True
        assert cb.last_failure_time is None
        assert cb.total_trips == 0

    def test_invalid_parameters_raise(self):
        with pytest.raises(ValueError, match="failure_threshold"):
            MCPCircuitBreaker(failure_threshold=0)
        with pytest.raises(ValueError, match="recovery_time"):
            MCPCircuitBreaker(recovery_time=0.0)

    def test_success_maintains_closed_state(self, mock_clock):
        cb = MCPCircuitBreaker(failure_threshold=3, recovery_time=10.0, time_provider=mock_clock)
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.allow_request() is True

    def test_interleaved_failures_and_success_resets_counter(self, mock_clock):
        cb = MCPCircuitBreaker(failure_threshold=3, recovery_time=10.0, time_provider=mock_clock)
        cb.record_failure()
        assert cb.failure_count == 1
        assert cb.state == CircuitState.CLOSED

        cb.record_failure()
        assert cb.failure_count == 2
        assert cb.state == CircuitState.CLOSED

        cb.record_success()
        assert cb.failure_count == 0
        assert cb.state == CircuitState.CLOSED

        # Next failure starts from 1, not tripping to OPEN
        cb.record_failure()
        assert cb.failure_count == 1
        assert cb.state == CircuitState.CLOSED

    def test_reaching_failure_threshold_trips_to_open(self, mock_clock):
        cb = MCPCircuitBreaker(failure_threshold=3, recovery_time=10.0, time_provider=mock_clock)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True

        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.failure_count == 3
        assert cb.total_trips == 1
        assert cb.allow_request() is False
        assert cb.last_failure_time == mock_clock.current_time

    def test_open_state_blocks_requests_before_recovery_time(self, mock_clock):
        cb = MCPCircuitBreaker(failure_threshold=2, recovery_time=15.0, time_provider=mock_clock)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        mock_clock.advance(5.0)
        assert cb.allow_request() is False
        assert cb.state == CircuitState.OPEN

        mock_clock.advance(9.9)
        assert cb.allow_request() is False
        assert cb.state == CircuitState.OPEN

    def test_reaches_half_open_after_recovery_time(self, mock_clock):
        cb = MCPCircuitBreaker(failure_threshold=2, recovery_time=15.0, time_provider=mock_clock)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        mock_clock.advance(15.0)
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.allow_request() is True

    def test_half_open_success_recovers_to_closed(self, mock_clock):
        cb = MCPCircuitBreaker(failure_threshold=2, recovery_time=10.0, time_provider=mock_clock)
        cb.record_failure()
        cb.record_failure()
        mock_clock.advance(10.0)
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.allow_request() is True

    def test_half_open_failure_retrips_to_open(self, mock_clock):
        cb = MCPCircuitBreaker(failure_threshold=2, recovery_time=10.0, time_provider=mock_clock)
        cb.record_failure()
        cb.record_failure()
        mock_clock.advance(10.0)
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.total_trips == 2
        assert cb.allow_request() is False

    def test_manual_reset(self, mock_clock):
        cb = MCPCircuitBreaker(failure_threshold=1, recovery_time=10.0, time_provider=mock_clock)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.last_failure_time is None
        assert cb.allow_request() is True

    @pytest.mark.asyncio
    async def test_call_executes_and_records_success(self, mock_clock):
        cb = MCPCircuitBreaker(failure_threshold=2, recovery_time=10.0, time_provider=mock_clock)

        async def dummy_op(val: int) -> int:
            return val * 2

        res = await cb.call(dummy_op, 5)
        assert res == 10
        assert cb.failure_count == 0

    @pytest.mark.asyncio
    async def test_call_blocks_when_circuit_open(self, mock_clock):
        cb = MCPCircuitBreaker(failure_threshold=1, recovery_time=10.0, time_provider=mock_clock)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        async def dummy_op():
            return "ok"

        with pytest.raises(CircuitOpenError, match="MCPCircuitBreaker is OPEN"):
            await cb.call(dummy_op)

    @pytest.mark.asyncio
    async def test_async_context_manager_lifecycle(self, mock_clock):
        cb = MCPCircuitBreaker(failure_threshold=1, recovery_time=10.0, time_provider=mock_clock)
        async with cb:
            pass
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

        with pytest.raises(ValueError):
            async with cb:
                raise ValueError("Operation failed")

        assert cb.state == CircuitState.OPEN
        assert cb.failure_count == 1


# ==============================================================================
# 3. LocalLoopGuard Unit Tests
# ==============================================================================

class TestLocalLoopGuard:
    def test_initialization_defaults(self):
        guard = LocalLoopGuard()
        assert guard.max_iterations == 50
        assert guard.timeout_seconds == 15.0
        assert guard.circuit_breaker is None
        assert guard.repository is None
        assert guard.current_iteration == 0
        assert guard.can_continue() is True
        assert guard.total_completed == 0
        assert guard.total_timeouts == 0
        assert guard.total_skipped == 0
        assert guard.total_errors == 0

    def test_invalid_parameters_raise(self):
        with pytest.raises(ValueError, match="max_iterations"):
            LocalLoopGuard(max_iterations=0)
        with pytest.raises(ValueError, match="timeout_seconds"):
            LocalLoopGuard(timeout_seconds=-1.0)

    def test_iteration_tracking_and_limit_check(self):
        guard = LocalLoopGuard(max_iterations=3)
        assert guard.can_continue() is True
        guard.check_iteration_limit()

        assert guard.increment_iteration() == 1
        assert guard.increment_iteration() == 2
        assert guard.can_continue() is True

        assert guard.increment_iteration() == 3
        assert guard.can_continue() is False
        with pytest.raises(MaxIterationsReachedError, match="Iteration limit reached"):
            guard.check_iteration_limit()

    def test_reset_clears_metrics(self):
        guard = LocalLoopGuard(max_iterations=10)
        guard.increment_iteration()
        guard.total_completed = 5
        guard.total_timeouts = 2
        guard.total_skipped = 2
        guard.total_errors = 1

        guard.reset()
        assert guard.current_iteration == 0
        assert guard.total_completed == 0
        assert guard.total_timeouts == 0
        assert guard.total_skipped == 0
        assert guard.total_errors == 0

    @pytest.mark.asyncio
    async def test_run_guarded_success(self, sample_job):
        guard = LocalLoopGuard(max_iterations=10, timeout_seconds=5.0)

        async def fast_step(job: JobPosting, score: int) -> int:
            job.status = JobStatus.TRIAGED
            return score * 2

        result = await guard.run_guarded(fast_step, sample_job, score=42, job=sample_job)
        assert result == 84
        assert sample_job.status == JobStatus.TRIAGED
        assert guard.current_iteration == 1
        assert guard.total_completed == 1
        assert guard.total_timeouts == 0
        assert guard.total_skipped == 0

    @pytest.mark.asyncio
    async def test_run_guarded_catches_asyncio_timeout_and_skips(self, sample_job, mock_repository):
        guard = LocalLoopGuard(
            max_iterations=5,
            timeout_seconds=0.05,
            repository=mock_repository,
        )

        async def hanging_step(job: JobPosting):
            await asyncio.sleep(1.0)
            return "never_reached"

        result = await guard.run_guarded(hanging_step, sample_job, job=sample_job)

        # Must cleanly return None without crashing
        assert result is None
        assert sample_job.status == JobStatus.SKIPPED_TIMEOUT
        assert "Execution timed out" in (sample_job.error_message or "")
        assert guard.total_timeouts == 1
        assert guard.total_skipped == 1
        assert guard.total_completed == 0

        # Repository should be notified immediately
        mock_repository.update_status.assert_awaited_once_with(
            job_id=sample_job.id,
            status=JobStatus.SKIPPED_TIMEOUT,
            error_message=sample_job.error_message,
        )

    @pytest.mark.asyncio
    async def test_run_guarded_catches_llm_timeout_error(self, sample_job, mock_repository):
        guard = LocalLoopGuard(
            max_iterations=5,
            timeout_seconds=5.0,
            repository=mock_repository,
        )

        async def llm_timeout_step():
            raise LLMTimeoutError("Ollama inference timed out after 30s")

        result = await guard.run_guarded(llm_timeout_step, job=sample_job)

        assert result is None
        assert sample_job.status == JobStatus.SKIPPED_TIMEOUT
        assert "LLMTimeoutError" in (sample_job.error_message or "") or "timed out" in (sample_job.error_message or "")
        assert guard.total_timeouts == 1
        assert guard.total_skipped == 1

        mock_repository.update_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_guarded_auto_extracts_job_from_positional_args(self, sample_job):
        guard = LocalLoopGuard(max_iterations=5, timeout_seconds=0.02)

        async def slow_step(job: JobPosting):
            await asyncio.sleep(0.5)

        # Do not explicitly pass job=sample_job; guard should detect it in args
        result = await guard.run_guarded(slow_step, sample_job)

        assert result is None
        assert sample_job.status == JobStatus.SKIPPED_TIMEOUT
        assert guard.total_skipped == 1

    @pytest.mark.asyncio
    async def test_run_guarded_max_iterations_enforced(self):
        guard = LocalLoopGuard(max_iterations=2, timeout_seconds=1.0)

        async def step():
            return "ok"

        res1 = await guard.run_guarded(step)
        assert res1 == "ok"
        assert guard.current_iteration == 1

        res2 = await guard.run_guarded(step)
        assert res2 == "ok"
        assert guard.current_iteration == 2

        with pytest.raises(MaxIterationsReachedError):
            await guard.run_guarded(step)

    @pytest.mark.asyncio
    async def test_run_guarded_propagates_non_timeout_exceptions(self, sample_job):
        guard = LocalLoopGuard(max_iterations=5, timeout_seconds=1.0)

        async def faulty_step():
            raise ValueError("Invalid job data schema")

        with pytest.raises(ValueError, match="Invalid job data schema"):
            await guard.run_guarded(faulty_step, job=sample_job)

        assert sample_job.status == JobStatus.INGESTED  # Not updated to SKIPPED_TIMEOUT
        assert guard.total_errors == 1
        assert guard.total_timeouts == 0

    @pytest.mark.asyncio
    async def test_circuit_breaker_integration_blocks_when_open(self, sample_job, mock_clock):
        cb = MCPCircuitBreaker(failure_threshold=1, recovery_time=30.0, time_provider=mock_clock)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        guard = LocalLoopGuard(max_iterations=5, circuit_breaker=cb)

        async def step():
            return "ok"

        with pytest.raises(CircuitOpenError):
            await guard.run_guarded(step, job=sample_job)

        assert sample_job.status == JobStatus.ERROR
        assert "Circuit breaker is OPEN" in (sample_job.error_message or "")

    @pytest.mark.asyncio
    async def test_circuit_breaker_records_success_and_failure(self, mock_clock):
        cb = MCPCircuitBreaker(failure_threshold=2, recovery_time=30.0, time_provider=mock_clock)
        guard = LocalLoopGuard(max_iterations=5, circuit_breaker=cb)

        async def good_step():
            return "ok"

        async def bad_step():
            raise RuntimeError("Database connection lost")

        # Success keeps breaker clean
        await guard.run_guarded(good_step)
        assert cb.failure_count == 0

        # Failure increments breaker count
        with pytest.raises(RuntimeError):
            await guard.run_guarded(bad_step)
        assert cb.failure_count == 1
        assert cb.state == CircuitState.CLOSED

        # Second failure trips breaker
        with pytest.raises(RuntimeError):
            await guard.run_guarded(bad_step)
        assert cb.failure_count == 2
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_multiple_jobs_loop_skips_timed_out_and_completes_healthy(self):
        """Simulate real DAG batch processing with mixed fast and hung jobs."""
        guard = LocalLoopGuard(max_iterations=10, timeout_seconds=0.05)

        jobs = [
            JobPosting(id=f"job-{i}", content_hash=f"{i}" * 64, title=f"Job {i}", company="Co", raw_description="Desc")
            for i in range(4)
        ]

        async def process(job: JobPosting, idx: int):
            if idx == 1 or idx == 2:
                await asyncio.sleep(0.5)  # Will time out
            job.status = JobStatus.SHORTLISTED
            return job

        results = []
        for i, j in enumerate(jobs):
            if not guard.can_continue():
                break
            res = await guard.run_guarded(process, j, i, job=j)
            results.append(res)

        # Job 0 and 3 succeeded; Job 1 and 2 timed out and were skipped
        assert results[0] is not None
        assert results[0].status == JobStatus.SHORTLISTED
        assert results[1] is None
        assert jobs[1].status == JobStatus.SKIPPED_TIMEOUT
        assert results[2] is None
        assert jobs[2].status == JobStatus.SKIPPED_TIMEOUT
        assert results[3] is not None
        assert results[3].status == JobStatus.SHORTLISTED

        assert guard.total_completed == 2
        assert guard.total_timeouts == 2
        assert guard.total_skipped == 2
        assert guard.current_iteration == 4
```

---

## 4. Integration Specifications for M3 Peer Modules

### Integration with `pipeline.py` (M3 Explorer 2)
In `JobPipelineOrchestrator`:
```python
class JobPipelineOrchestrator:
    def __init__(
        self,
        repository: JobRepository,
        evaluator: JobFitEvaluator,
        truncator: TextTruncator,
        mcp_client: BaseJobIngestionClient,
        guard: Optional[LocalLoopGuard] = None,
        circuit_breaker: Optional[MCPCircuitBreaker] = None,
    ) -> None:
        self.repository = repository
        self.evaluator = evaluator
        self.truncator = truncator
        self.mcp_client = mcp_client
        self.circuit_breaker = circuit_breaker or MCPCircuitBreaker(failure_threshold=3, recovery_time=30.0)
        self.guard = guard or LocalLoopGuard(
            max_iterations=50,
            timeout_seconds=15.0,
            circuit_breaker=self.circuit_breaker,
            repository=self.repository,
        )

    async def ingest_jobs(self, limit: int = 10) -> list[JobPosting]:
        # MCP calls protected by circuit breaker
        async with self.circuit_breaker:
            return await self.mcp_client.fetch_jobs(limit=limit)

    async def triage_job(self, job: JobPosting, candidate_profile: CandidateProfile) -> Optional[JobPosting]:
        # Guarded LLM evaluation with timeout protection & SKIPPED_TIMEOUT auto-marking
        eval_result = await self.guard.run_guarded(
            self.evaluator.evaluate_fit,
            job.cleaned_description or job.raw_description,
            candidate_profile,
            job=job,
            step_name="llm_triage",
        )
        if eval_result is None:
            # Cleanly skipped on timeout; job is already marked SKIPPED_TIMEOUT
            return None

        # Apply triage results and transition status
        job.fit_score = eval_result.fit_score
        job.recommendation = eval_result.recommendation
        job.evaluation = eval_result
        job.status = JobStatus.SHORTLISTED if eval_result.recommendation == Recommendation.SHORTLIST else JobStatus.DISCARDED
        await self.repository.update_status(
            job_id=job.id,
            status=job.status,
            fit_score=job.fit_score,
            recommendation=job.recommendation,
        )
        return job
```

### Export Configuration for `src/core/__init__.py`
`src/core/__init__.py` should be updated to export:
```python
from src.core.harness import (
    CircuitOpenError,
    CircuitState,
    HarnessError,
    LocalLoopGuard,
    MaxIterationsReachedError,
    MCPCircuitBreaker,
)
from src.core.truncator import DescriptionTooShortError, TextTruncator, TruncationResult

__all__ = [
    "CircuitOpenError",
    "CircuitState",
    "DescriptionTooShortError",
    "HarnessError",
    "LocalLoopGuard",
    "MaxIterationsReachedError",
    "MCPCircuitBreaker",
    "TextTruncator",
    "TruncationResult",
]
```
