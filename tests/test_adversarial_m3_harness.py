"""
Empirical Adversarial Stress Test Suite for Milestone M3: Execution Harness & Pipeline.

Tested by M3 Challenger 1.
Covers:
1. LocalLoopGuard extreme timeout recovery (microsecond timeouts, simulated slow coroutines,
   TimeoutError, LLMTimeoutError, DuckDB persistence, graceful skip without crash).
2. MCPCircuitBreaker resilience & state transitions (rapid consecutive failures, OPEN rejection,
   time-based HALF_OPEN transition, trial probe success/failure, concurrent load, trip counting).
3. Max iterations boundary enforcement (0, negative, exact boundary, pipeline halting) and
   exception propagation for non-timeout errors (ValueError, RuntimeError, CancelledError).
4. Full pipeline resilience under mixed fault injection (timeouts, duplicates, short descriptions,
   circuit breaker trips, and O(1) memory streaming).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import tracemalloc
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.use_cases.harness import (
    CircuitOpenError,
    CircuitState,
    LocalLoopGuard,
    MaxIterationsReachedError,
    MCPCircuitBreaker,
)
from src.application.use_cases.pipeline import (
    JobPipeline,
    PipelineConfig,
    PipelineEventType,
)
from src.infrastructure.adapters.repository import JobRepository, compute_job_hash
from src.infrastructure.adapters.llm_evaluator import JobFitEvaluator, LLMTimeoutError
from src.infrastructure.adapters.mcp_mock_client import MockMcpJobClient
from src.domain.models import (
    JobPosting,
    JobStatus,
    MatchEvaluation,
    Recommendation,
)

# ==============================================================================
# Helper Classes & Fixtures
# ==============================================================================

class ControllableClock:
    """Deterministic monotonic clock provider."""

    def __init__(self, initial_time: float = 1000.0) -> None:
        self._time = initial_time

    def __call__(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds


@pytest.fixture
def controllable_clock() -> ControllableClock:
    return ControllableClock()


@pytest.fixture
async def temp_duckdb_repo():
    """Provides an isolated real DuckDB JobRepository on disk."""
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "m3_stress.duckdb")
    repo = JobRepository(db_path=db_path)
    await repo.initialize()
    yield repo, db_path
    await repo.close()
    for p in (db_path, f"{db_path}.wal"):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    try:
        os.rmdir(temp_dir)
    except OSError:
        pass


def make_test_job(
    job_id: str,
    title: str = "Backend Systems Engineer",
    company: str = "TestCorp",
    raw_description: str | None = None,
) -> JobPosting:
    if raw_description is None:
        raw_description = (
            f"Title: {title}. Company: {company}. ID: {job_id}. "
            "Seeking experienced Python distributed systems engineer. "
            "Must know FastAPI, DuckDB, Asyncio, and Docker. 5+ years experience required."
        )
    chash = compute_job_hash(raw_description, title=title, company=company)
    return JobPosting(
        id=job_id,
        content_hash=chash,
        title=title,
        company=company,
        raw_description=raw_description,
        status=JobStatus.INGESTED,
    )


# ==============================================================================
# 1. LocalLoopGuard Extreme Timeout Recovery Stress Tests
# ==============================================================================

class TestLocalLoopGuardExtremeTimeouts:
    """Stress tests LocalLoopGuard timeout recovery under extreme conditions."""

    @pytest.mark.asyncio
    async def test_microsecond_timeout_recovery(self, temp_duckdb_repo):
        """Verify 0.001s timeout catches slow coroutine, updates DuckDB and skips gracefully."""
        repo, _ = temp_duckdb_repo
        job = make_test_job("job-micro-1")
        await repo.save_job(job)

        guard = LocalLoopGuard(
            max_iterations=10,
            timeout_seconds=0.001,  # 1 millisecond
            repository=repo,
        )

        async def slow_work(job: JobPosting):
            await asyncio.sleep(0.05)
            return "unexpected_success"

        result = await guard.run_guarded(slow_work, job=job)

        # Assert returned None gracefully
        assert result is None
        # Assert in-memory job updated
        assert job.status == JobStatus.SKIPPED_TIMEOUT
        assert "Execution timed out" in (job.error_message or "")

        # Assert guard telemetry updated
        assert guard.total_timeouts == 1
        assert guard.total_skipped == 1
        assert guard.total_completed == 0
        assert guard.current_iteration == 1

        # Assert persisted to DuckDB
        db_job = await repo.get_job("job-micro-1")
        assert db_job is not None
        assert db_job.status == JobStatus.SKIPPED_TIMEOUT
        assert "Execution timed out" in (db_job.error_message or "")

    @pytest.mark.asyncio
    async def test_explicit_timeout_error_and_llm_timeout_error(self, temp_duckdb_repo):
        """Verify both standard TimeoutError and LLMTimeoutError are caught and handled identically."""
        repo, _ = temp_duckdb_repo
        job1 = make_test_job("job-timeout-err", title="Standard Timeout Role")
        job2 = make_test_job("job-llm-timeout-err", title="LLM Timeout Role")
        saved1 = await repo.save_job(job1)
        saved2 = await repo.save_job(job2)
        assert saved1 is True
        assert saved2 is True

        guard = LocalLoopGuard(max_iterations=5, timeout_seconds=10.0, repository=repo)

        async def raise_std_timeout():
            raise TimeoutError("Simulated stdlib timeout")

        async def raise_llm_timeout():
            raise LLMTimeoutError("Simulated LLM inference timeout")

        # 1. Standard TimeoutError
        res1 = await guard.run_guarded(raise_std_timeout, job=job1)
        assert res1 is None
        assert job1.status == JobStatus.SKIPPED_TIMEOUT
        db_job1 = await repo.get_job("job-timeout-err")
        assert db_job1 is not None
        assert db_job1.status == JobStatus.SKIPPED_TIMEOUT

        # 2. LLMTimeoutError
        res2 = await guard.run_guarded(raise_llm_timeout, job=job2)
        assert res2 is None
        assert job2.status == JobStatus.SKIPPED_TIMEOUT
        db_job2 = await repo.get_job("job-llm-timeout-err")
        assert db_job2 is not None
        assert db_job2.status == JobStatus.SKIPPED_TIMEOUT

        assert guard.total_timeouts == 2
        assert guard.total_skipped == 2
        assert guard.total_completed == 0

    @pytest.mark.asyncio
    async def test_duckdb_update_failure_does_not_crash_guard(self):
        """Verify if DuckDB fails during timeout status persistence, guard logs and returns None."""
        failing_repo = MagicMock()
        failing_repo.update_status = AsyncMock(side_effect=RuntimeError("DuckDB lock failure"))

        job = make_test_job("job-db-fail")
        guard = LocalLoopGuard(max_iterations=5, timeout_seconds=0.001, repository=failing_repo)

        async def slow_work():
            await asyncio.sleep(0.05)

        # Must NOT raise RuntimeError from failing_repo; must absorb and return None
        result = await guard.run_guarded(slow_work, job=job)
        assert result is None
        assert job.status == JobStatus.SKIPPED_TIMEOUT
        assert guard.total_timeouts == 1
        assert guard.total_skipped == 1

    @pytest.mark.asyncio
    async def test_timeout_without_job_entity(self):
        """Verify timeout guard functions without a JobPosting instance."""
        guard = LocalLoopGuard(max_iterations=5, timeout_seconds=0.001)

        async def slow_anon_work():
            await asyncio.sleep(0.05)

        result = await guard.run_guarded(slow_anon_work)
        assert result is None
        assert guard.total_timeouts == 1
        assert guard.total_skipped == 1

    @pytest.mark.asyncio
    async def test_rapid_alternating_timeouts_and_successes(self, temp_duckdb_repo):
        """Verify multi-job execution interleaving fast and timed-out steps."""
        repo, _ = temp_duckdb_repo
        guard = LocalLoopGuard(max_iterations=20, timeout_seconds=0.01, repository=repo)

        results = []
        for i in range(10):
            job = make_test_job(f"job-alt-{i}", title=f"Role {i}")
            await repo.save_job(job)

            async def step(idx: int, is_slow: bool, job: JobPosting):
                if is_slow:
                    await asyncio.sleep(0.05)
                    return "slow"
                return f"fast_{idx}"

            is_slow = (i % 2 == 1)
            res = await guard.run_guarded(step, i, is_slow, job=job)
            results.append(res)

        assert results == [
            "fast_0", None, "fast_2", None, "fast_4",
            None, "fast_6", None, "fast_8", None,
        ]
        assert guard.total_completed == 5
        assert guard.total_timeouts == 5
        assert guard.total_skipped == 5
        assert guard.current_iteration == 10


# ==============================================================================
# 2. MCPCircuitBreaker Adversarial State Machine & Stress Tests
# ==============================================================================

class TestMCPCircuitBreakerAdversarial:
    """Stress tests MCPCircuitBreaker state machine, concurrency, and edge conditions."""

    def test_failure_threshold_tripping_to_open(self, controllable_clock):
        """Verify circuit trips to OPEN on reaching failure threshold and blocks further requests."""
        cb = MCPCircuitBreaker(
            failure_threshold=3,
            recovery_time=30.0,
            time_provider=controllable_clock,
        )

        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True
        assert cb.total_trips == 0

        # 3rd failure reaches threshold
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False
        assert cb.total_trips == 1

    def test_record_failure_while_already_open_defect_reproduction(self, controllable_clock):
        """
        EMPIRICAL DEFECT TEST:
        Demonstrates that calling record_failure() when state is already OPEN
        erroneously increments total_trips and treats existing OPEN as a new transition.
        """
        cb = MCPCircuitBreaker(
            failure_threshold=3,
            recovery_time=30.0,
            time_provider=controllable_clock,
        )
        for _ in range(3):
            cb.record_failure()

        assert cb.state == CircuitState.OPEN
        assert cb.total_trips == 1

        # In a defect-free circuit breaker, calling record_failure() while already OPEN
        # should update timestamp but NOT increment total_trips.
        # Under current implementation, total_trips increments on every failure call while OPEN:
        initial_trips = cb.total_trips
        cb.record_failure()
        # Document actual behavior:
        trips_after = cb.total_trips
        assert trips_after == initial_trips + 1, "Implementation increments total_trips while already OPEN"

    def test_clock_boundary_recovery_to_half_open(self, controllable_clock):
        """Verify strict time boundary between OPEN and HALF_OPEN."""
        cb = MCPCircuitBreaker(
            failure_threshold=2,
            recovery_time=10.0,
            time_provider=controllable_clock,
        )
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        # 9.999 seconds: still OPEN
        controllable_clock.advance(9.999)
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False

        # 10.000 seconds: transitions to HALF_OPEN
        controllable_clock.advance(0.001)
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.allow_request() is True

    def test_half_open_trial_probe_success_resets_to_closed(self, controllable_clock):
        """Verify successful probe in HALF_OPEN restores CLOSED state and clears failure count."""
        cb = MCPCircuitBreaker(
            failure_threshold=3,
            recovery_time=15.0,
            time_provider=controllable_clock,
        )
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

        controllable_clock.advance(15.0)
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.allow_request() is True

    def test_half_open_trial_probe_failure_retrips_to_open(self, controllable_clock):
        """Verify failed probe in HALF_OPEN immediately re-trips to OPEN and increments total_trips."""
        cb = MCPCircuitBreaker(
            failure_threshold=3,
            recovery_time=15.0,
            time_provider=controllable_clock,
        )
        for _ in range(3):
            cb.record_failure()
        assert cb.total_trips == 1

        controllable_clock.advance(15.0)
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.total_trips == 2
        assert cb.allow_request() is False

        # Needs full recovery time again
        controllable_clock.advance(14.9)
        assert cb.state == CircuitState.OPEN
        controllable_clock.advance(0.2)
        assert cb.state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_rejection_behavior_when_open(self, controllable_clock):
        """Verify call() and context manager raise CircuitOpenError when OPEN without executing op."""
        cb = MCPCircuitBreaker(
            failure_threshold=1,
            recovery_time=10.0,
            time_provider=controllable_clock,
        )
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        executed = False

        async def target():
            nonlocal executed
            executed = True
            return "data"

        # 1. call() rejection
        with pytest.raises(CircuitOpenError, match="MCPCircuitBreaker is OPEN"):
            await cb.call(target)
        assert not executed

        # 2. Context manager rejection
        with pytest.raises(CircuitOpenError, match="MCPCircuitBreaker is OPEN"):
            async with cb:
                executed = True
        assert not executed

    @pytest.mark.asyncio
    async def test_concurrent_call_rejections_under_load(self, controllable_clock):
        """Stress 50 concurrent requests hitting OPEN circuit breaker simultaneously."""
        cb = MCPCircuitBreaker(
            failure_threshold=1,
            recovery_time=100.0,
            time_provider=controllable_clock,
        )
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        mock_op = AsyncMock(return_value="res")

        async def worker():
            with pytest.raises(CircuitOpenError):
                await cb.call(mock_op)

        tasks = [asyncio.create_task(worker()) for _ in range(50)]
        await asyncio.gather(*tasks)

        # Target operation should never have been invoked
        mock_op.assert_not_called()

    def test_multi_cycle_oscillation_stress(self, controllable_clock):
        """Stress test 3 continuous cycles of trip -> recover -> trip -> recover."""
        cb = MCPCircuitBreaker(
            failure_threshold=2,
            recovery_time=5.0,
            time_provider=controllable_clock,
        )

        for cycle in range(3):
            # Trip to OPEN
            cb.record_failure()
            cb.record_failure()
            assert cb.state == CircuitState.OPEN

            # Advance to HALF_OPEN
            controllable_clock.advance(5.0)
            assert cb.state == CircuitState.HALF_OPEN

            # Probe succeeds -> CLOSED
            cb.record_success()
            assert cb.state == CircuitState.CLOSED
            assert cb.failure_count == 0

        assert cb.state == CircuitState.CLOSED
        assert cb.total_trips == 3


# ==============================================================================
# 3. Max Iterations & Exception Propagation Stress Tests
# ==============================================================================

class TestMaxIterationsAndExceptionPropagation:
    """Stress tests boundary limits and non-timeout exception propagation."""

    def test_invalid_guard_parameters_raise(self):
        """Verify boundary violations for max_iterations and timeout_seconds."""
        with pytest.raises(ValueError, match="max_iterations"):
            LocalLoopGuard(max_iterations=0)
        with pytest.raises(ValueError, match="max_iterations"):
            LocalLoopGuard(max_iterations=-5)
        with pytest.raises(ValueError, match="timeout_seconds"):
            LocalLoopGuard(timeout_seconds=0.0)
        with pytest.raises(ValueError, match="timeout_seconds"):
            LocalLoopGuard(timeout_seconds=-0.5)

    @pytest.mark.asyncio
    async def test_exact_iteration_exhaustion_at_boundary(self):
        """Verify guard enforces limit exactly at max_iterations."""
        guard = LocalLoopGuard(max_iterations=3, timeout_seconds=5.0)

        async def noop():
            return "done"

        assert guard.can_continue() is True
        res1 = await guard.run_guarded(noop)
        assert res1 == "done"
        assert guard.current_iteration == 1
        assert guard.can_continue() is True

        res2 = await guard.run_guarded(noop)
        assert res2 == "done"
        assert guard.current_iteration == 2
        assert guard.can_continue() is True

        res3 = await guard.run_guarded(noop)
        assert res3 == "done"
        assert guard.current_iteration == 3
        assert guard.can_continue() is False

        # 4th call must raise MaxIterationsReachedError
        with pytest.raises(MaxIterationsReachedError, match="Iteration limit reached: 3/3"):
            await guard.run_guarded(noop)

        assert guard.current_iteration == 3
        assert guard.total_completed == 3

    @pytest.mark.asyncio
    async def test_non_timeout_exceptions_are_re_raised(self):
        """Verify non-timeout exceptions (ValueError, RuntimeError, etc.) are strictly propagated."""
        guard = LocalLoopGuard(max_iterations=5, timeout_seconds=5.0)

        async def throw_val_error():
            raise ValueError("Corrupted job payload")

        async def throw_runtime_error():
            raise RuntimeError("Database connection severed")

        with pytest.raises(ValueError, match="Corrupted job payload"):
            await guard.run_guarded(throw_val_error)
        assert guard.total_errors == 1

        with pytest.raises(RuntimeError, match="Database connection severed"):
            await guard.run_guarded(throw_runtime_error)
        assert guard.total_errors == 2

        # Completed and timeouts remain 0
        assert guard.total_completed == 0
        assert guard.total_timeouts == 0

    @pytest.mark.asyncio
    async def test_circuit_breaker_notified_on_non_timeout_exception(self):
        """Verify attached circuit breaker records failure when step raises non-timeout error."""
        cb = MCPCircuitBreaker(failure_threshold=2, recovery_time=10.0)
        guard = LocalLoopGuard(max_iterations=5, timeout_seconds=5.0, circuit_breaker=cb)

        async def fail_step():
            raise RuntimeError("MCP RPC failure")

        with pytest.raises(RuntimeError):
            await guard.run_guarded(fail_step)

        assert cb.failure_count == 1
        assert cb.state == CircuitState.CLOSED

        with pytest.raises(RuntimeError):
            await guard.run_guarded(fail_step)

        assert cb.failure_count == 2
        assert cb.state == CircuitState.OPEN

        # Next call blocked by circuit breaker
        with pytest.raises(CircuitOpenError):
            await guard.run_guarded(fail_step)

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates_unhindered(self):
        """Verify asyncio.CancelledError is never swallowed or converted to timeout."""
        guard = LocalLoopGuard(max_iterations=5, timeout_seconds=5.0)

        async def cancelled_task():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await guard.run_guarded(cancelled_task)

        # CancelledError is BaseException, not standard Exception: total_errors should not increment
        assert guard.total_timeouts == 0
        assert guard.total_skipped == 0


# ==============================================================================
# 4. End-to-End Pipeline Fault & Resilience Stress Tests
# ==============================================================================

class TestPipelineEndToEndStress:
    """Stress tests full closed-loop pipeline under adversarial faults."""

    @pytest.mark.asyncio
    async def test_pipeline_halts_at_guard_iteration_limit(self, temp_duckdb_repo):
        """Verify pipeline stops processing batch once guard iteration limit is reached."""
        repo, _ = temp_duckdb_repo
        guard = LocalLoopGuard(max_iterations=2, timeout_seconds=5.0, repository=repo)

        jobs = [make_test_job(f"job-limit-{i}", title=f"Title {i}") for i in range(10)]
        mock_client = MockMcpJobClient(jobs=jobs, require_connection=False)

        evaluator = MagicMock(spec=JobFitEvaluator)
        evaluator.evaluate_fit = AsyncMock(
            return_value=MatchEvaluation(
                fit_score=85,
                recommendation=Recommendation.SHORTLIST,
                matched_skills=["Python"],
                missing_skills=[],
            )
        )

        pipeline = JobPipeline(
            repository=repo,
            evaluator=evaluator,
            ingestion_client=mock_client,
            guard=guard,
            config=PipelineConfig(batch_size=5, max_iterations=10),
        )

        events: list[PipelineEventType] = []
        pipeline.add_listener(lambda e: events.append(e.event_type))

        result = await pipeline.run(limit=10)

        # Should have stopped after 2 jobs because guard.max_iterations=2
        assert result.total_processed == 2
        assert result.shortlisted == 2
        assert PipelineEventType.PIPELINE_STOPPED in events
        assert PipelineEventType.PIPELINE_COMPLETED in events

    @pytest.mark.asyncio
    async def test_pipeline_mixed_batch_with_timeouts_and_duplicates(self, temp_duckdb_repo):
        """
        Verify pipeline handles mixed batch of:
        - normal shortlisted job
        - normal discarded job
        - duplicate job
        - LLM timeout job
        - short description failed job
        All without crashing and persisting correct states to DuckDB.
        """
        repo, _ = temp_duckdb_repo
        guard = LocalLoopGuard(max_iterations=10, timeout_seconds=0.01, repository=repo)

        # Pre-seed duplicate in DuckDB
        dup_job = make_test_job("dup-seeded", title="Duplicate Title", raw_description="Duplicate job description exact text")
        await repo.save_job(dup_job)

        jobs = [
            # Job 0: Normal high fit -> SHORTLISTED
            make_test_job("job-shortlist", title="High Fit Python Engineer", raw_description="High Fit Python Engineer: FastAPI, DuckDB, Asyncio expert."),
            # Job 1: Duplicate -> DUPLICATE
            make_test_job("job-dup", title="Duplicate Title", raw_description="Duplicate job description exact text"),
            # Job 2: Slow LLM -> SKIPPED_TIMEOUT
            make_test_job("job-timeout", title="Timeout Python Engineer", raw_description="Timeout Python Engineer: Long running inference test description."),
            # Job 3: Normal low fit -> DISCARDED
            make_test_job("job-discard", title="Low Fit Python Engineer", raw_description="Low Fit Python Engineer: Junior HTML and CSS role with zero Python."),
            # Job 4: Short description -> FAILED
            JobPosting(
                id="job-short-desc",
                content_hash="short" * 12 + "1234",
                title="Tiny Desc",
                company="TinyCorp",
                raw_description="Too short",
                status=JobStatus.INGESTED,
            ),
        ]
        mock_client = MockMcpJobClient(jobs=jobs, require_connection=False)

        evaluator = MagicMock(spec=JobFitEvaluator)

        async def mock_eval(desc: str, profile: Any) -> MatchEvaluation:
            if "Timeout Python Engineer" in desc:
                await asyncio.sleep(0.05)  # Triggers 0.01s guard timeout
            elif "Low Fit Python Engineer" in desc:
                return MatchEvaluation(
                    fit_score=35,
                    recommendation=Recommendation.DISCARD,
                    matched_skills=[],
                    missing_skills=["Python", "FastAPI"],
                )
            return MatchEvaluation(
                fit_score=90,
                recommendation=Recommendation.SHORTLIST,
                matched_skills=["Python", "FastAPI"],
                missing_skills=[],
            )

        evaluator.evaluate_fit = AsyncMock(side_effect=mock_eval)

        pipeline = JobPipeline(
            repository=repo,
            evaluator=evaluator,
            ingestion_client=mock_client,
            guard=guard,
            config=PipelineConfig(score_threshold=70, timeout_seconds=0.01),
        )

        result = await pipeline.run(limit=5)

        # Assert summary metrics
        assert result.total_processed == 5
        assert result.shortlisted == 1
        assert result.discarded == 1
        assert result.duplicates == 1
        assert result.skipped_timeout == 1
        assert result.errors == 1  # From short description

        # Verify DuckDB persistence directly
        shortlisted_db = await repo.get_job("job-shortlist")
        assert shortlisted_db is not None
        assert shortlisted_db.status == JobStatus.SHORTLISTED
        assert shortlisted_db.fit_score == 90

        discarded_db = await repo.get_job("job-discard")
        assert discarded_db is not None
        assert discarded_db.status == JobStatus.DISCARDED
        assert discarded_db.fit_score == 35

        timeout_db = await repo.get_job("job-timeout")
        assert timeout_db is not None
        assert timeout_db.status == JobStatus.SKIPPED_TIMEOUT

        short_db = await repo.get_job("job-short-desc")
        assert short_db is not None
        assert short_db.status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_streaming_ram_boundedness_under_high_volume(self, temp_duckdb_repo):
        """
        Stream 100 jobs through pipeline while monitoring peak memory via tracemalloc.
        Asserts memory does not grow unbounded with job count (O(1) memory requirement).
        """
        repo, _ = temp_duckdb_repo

        evaluator = MagicMock(spec=JobFitEvaluator)
        evaluator.evaluate_fit = AsyncMock(
            return_value=MatchEvaluation(
                fit_score=80,
                recommendation=Recommendation.SHORTLIST,
                matched_skills=["Python"],
                missing_skills=[],
            )
        )

        pipeline = JobPipeline(
            repository=repo,
            evaluator=evaluator,
            config=PipelineConfig(score_threshold=70),
        )

        async def stream_generator():
            for i in range(100):
                yield make_test_job(f"stream-job-{i}", title=f"Streaming Role {i}")

        tracemalloc.start()
        result = await pipeline.process_stream(stream_generator(), limit=100)
        _, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert result.total_processed == 100
        assert result.shortlisted == 100

        # Peak memory for processing 100 jobs should remain well bounded (< 15 MB)
        peak_mb = peak_mem / (1024 * 1024)
        assert peak_mb < 15.0, f"Peak memory {peak_mb:.2f} MB exceeded 15 MB ceiling"
