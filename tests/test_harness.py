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
    LocalLoopGuard,
    MaxIterationsReachedError,
    MCPCircuitBreaker,
)
from src.llm.evaluator import LLMTimeoutError
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
            if idx in (1, 2):
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
