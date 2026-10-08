"""
Tier 4: Real-World Resilience, Fault Tolerance & Full Pipeline Tests.

Validates robust execution under adverse operating conditions:
1. Graceful wall-clock timeout recovery:
   - Inference exceeding timeout_seconds is caught by LocalLoopGuard
   - Job is recorded in DuckDB as JobStatus.SKIPPED_TIMEOUT
   - Pipeline loop continues to next job without terminating
2. MCP Ingestion circuit breaker tripping:
   - Consecutive MCP server errors trip breaker to OPEN
   - Prevents system hanging or resource leak
3. Full closed-loop mixed-workload resilience:
   - Mixed stream containing matches, mismatches, duplicates, and slow jobs
   - Validates scalar telemetry summary and O(1) RAM flushing
4. Headless and TTY mode execution via Typer CLI
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from src.presentation.cli import app
from src.application.use_cases.harness import (
    CircuitOpenError,
    CircuitState,
    LocalLoopGuard,
    MCPCircuitBreaker,
)
from src.application.use_cases.pipeline import JobPipeline, PipelineConfig
from src.application.use_cases.truncator import TextTruncator
from src.infrastructure.adapters.repository import JobRepository
from src.infrastructure.adapters.llm_evaluator import JobFitEvaluator
from src.infrastructure.adapters.mcp_mock_client import MockMcpJobClient
from src.domain.models import (
    CandidateProfile,
    JobPosting,
    JobStatus,
    MatchEvaluation,
    Recommendation,
)

runner = CliRunner()


@pytest.fixture
def resilient_candidate() -> CandidateProfile:
    return CandidateProfile(
        name="Senior Python / AI Systems Engineer",
        target_role="Senior Python / AI Systems Engineer",
        years_experience=7,
        primary_skills=["Python 3.12", "AsyncIO", "DuckDB", "MCP", "Instructor"],
        secondary_skills=["Docker", "Linux"],
        summary="Senior Python AI Systems Engineer.",
    )


class TestTier4TimeoutResilience:
    """Validate wall-clock timeout catching, status marking, and loop continuation."""

    @pytest.mark.asyncio
    async def test_inference_timeout_marks_skipped_timeout_and_continues(
        self,
        tmp_path: Path,
        resilient_candidate: CandidateProfile,
    ):
        db_path = str(tmp_path / "timeout_resilience.duckdb")
        repo = JobRepository(db_path=db_path)
        await repo.initialize()

        # Job 1: Slow job that triggers timeout
        job1 = JobPosting(
            id="job-slow-01",
            content_hash="hash-slow-01",
            title="Senior Python AI Engineer",
            company="SlowCorp",
            raw_description="SlowCorp: Python distributed agent systems with AsyncIO and DuckDB.",
        )

        # Job 2: Fast job that succeeds immediately after
        job2 = JobPosting(
            id="job-fast-02",
            content_hash="hash-fast-02",
            title="Senior Python AI Engineer",
            company="FastCorp",
            raw_description="FastCorp: Python distributed agent systems with AsyncIO and DuckDB.",
        )

        mcp_client = MockMcpJobClient(jobs=[job1, job2])
        await mcp_client.connect()

        # Mock evaluator where Job 1 sleeps past timeout, and Job 2 returns immediately
        mock_client = MagicMock()

        async def _mock_inference(model, response_model, messages, **kwargs):
            user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
            if "SlowCorp" in user_msg:
                # Exceed the 0.05s timeout
                await asyncio.sleep(0.3)
            return MatchEvaluation(
                fit_score=85,
                recommendation=Recommendation.SHORTLIST,
                matched_skills=["Python", "AsyncIO"],
                missing_skills=[],
                reasoning="Fast job evaluated successfully.",
            )

        mock_client.chat.completions.create = AsyncMock(side_effect=_mock_inference)

        # Guard configured with tight timeout of 0.05 seconds
        guard = LocalLoopGuard(
            max_iterations=10,
            timeout_seconds=0.05,
            repository=repo,
        )
        evaluator = JobFitEvaluator(client=mock_client, score_threshold=70)
        truncator = TextTruncator(max_tokens=1500)

        pipeline = JobPipeline(
            repository=repo,
            evaluator=evaluator,
            ingestion_client=mcp_client,
            candidate_profile=resilient_candidate,
            truncator=truncator,
            guard=guard,
            config=PipelineConfig(score_threshold=70, timeout_seconds=0.05),
        )

        result = await pipeline.run()

        # Job 1 timed out and was skipped; Job 2 was shortlisted
        assert result.total_ingested == 2
        assert result.skipped_timeout == 1
        assert result.shortlisted == 1
        assert result.errors == 0

        # Verify DuckDB records
        persisted_job1 = await repo.get_job("job-slow-01")
        assert persisted_job1 is not None
        assert persisted_job1.status == JobStatus.SKIPPED_TIMEOUT
        assert "timed out" in (persisted_job1.error_message or "").lower()

        persisted_job2 = await repo.get_job("job-fast-02")
        assert persisted_job2 is not None
        assert persisted_job2.status == JobStatus.SHORTLISTED

        await repo.close()


class TestTier4CircuitBreakerResilience:
    """Validate MCP circuit breaker tripping on repeated failure."""

    @pytest.mark.asyncio
    async def test_mcp_circuit_breaker_trips_and_prevents_cascading_failure(self):
        breaker = MCPCircuitBreaker(failure_threshold=3, recovery_time=30.0)

        async def failing_fetch():
            raise ConnectionResetError("Remote MCP server crashed")

        # First 2 failures: breaker remains CLOSED
        for _ in range(2):
            with pytest.raises(ConnectionResetError):
                await breaker.call(failing_fetch)
            assert breaker.state == CircuitState.CLOSED

        # 3rd failure: trips to OPEN
        with pytest.raises(ConnectionResetError):
            await breaker.call(failing_fetch)
        assert breaker.state == CircuitState.OPEN

        # Subsequent attempts fail fast with CircuitOpenError without calling failing_fetch
        with pytest.raises(CircuitOpenError):
            await breaker.call(failing_fetch)

        assert breaker.allow_request() is False


class TestTier4ClosedLoopPipelineScenario:
    """Full end-to-end pipeline run on a heterogeneous workload."""

    @pytest.mark.asyncio
    async def test_closed_loop_mixed_workload_execution(
        self,
        tmp_path: Path,
        resilient_candidate: CandidateProfile,
    ):
        db_path = str(tmp_path / "closed_loop.duckdb")
        repo = JobRepository(db_path=db_path)
        await repo.initialize()

        # Seed 1 existing job to trigger deduplication
        existing_job = JobPosting(
            id="job-existing-01",
            content_hash="hash-existing-dup",
            title="Duplicate Role",
            company="DupCorp",
            raw_description="This job already exists in DuckDB.",
            status=JobStatus.SHORTLISTED,
        )
        await repo.save_job(existing_job)

        # Incoming stream: 1 match, 1 mismatch, 1 duplicate, 1 timeout
        job_match = JobPosting(
            id="job-stream-match",
            content_hash="hash-stream-match",
            title="Senior Python AI Engineer",
            company="MatchCorp",
            raw_description="Build autonomous agents using Python 3.12, DuckDB, and MCP.",
        )
        job_mismatch = JobPosting(
            id="job-stream-mismatch",
            content_hash="hash-stream-mismatch",
            title="ICU Registered Nurse",
            company="HospitalCorp",
            raw_description="HospitalCorp: Clinical ICU bedside patient nursing care.",
        )
        job_dup = JobPosting(
            id="job-stream-dup",
            content_hash="hash-existing-dup",
            title="Duplicate Role",
            company="DupCorp",
            raw_description="This job already exists in DuckDB.",
        )
        job_slow = JobPosting(
            id="job-stream-slow",
            content_hash="hash-stream-slow",
            title="Slow LLM Evaluation Job",
            company="SlowCorp",
            raw_description="SlowCorp: High-throughput evaluation pipeline that stalls and times out during inference.",
        )

        mcp_client = MockMcpJobClient(jobs=[job_match, job_mismatch, job_dup, job_slow])
        await mcp_client.connect()

        mock_client = MagicMock()

        async def _mock_eval(model, response_model, messages, **kwargs):
            user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
            if "SlowCorp" in user_msg:
                await asyncio.sleep(0.3)
            elif "HospitalCorp" in user_msg:
                return MatchEvaluation(
                    fit_score=15,
                    recommendation=Recommendation.DISCARD,
                    matched_skills=[],
                    missing_skills=["Python"],
                    reasoning="Non-technical nursing role.",
                )
            return MatchEvaluation(
                fit_score=90,
                recommendation=Recommendation.SHORTLIST,
                matched_skills=["Python", "DuckDB", "MCP"],
                missing_skills=[],
                reasoning="Exact skill alignment.",
            )

        mock_client.chat.completions.create = AsyncMock(side_effect=_mock_eval)

        guard = LocalLoopGuard(max_iterations=10, timeout_seconds=0.05, repository=repo)
        evaluator = JobFitEvaluator(client=mock_client, score_threshold=70)
        truncator = TextTruncator(max_tokens=1500)

        pipeline = JobPipeline(
            repository=repo,
            evaluator=evaluator,
            ingestion_client=mcp_client,
            candidate_profile=resilient_candidate,
            truncator=truncator,
            guard=guard,
            config=PipelineConfig(score_threshold=70, timeout_seconds=0.05),
        )

        result = await pipeline.run()

        # Breakdown assertions
        assert result.total_ingested == 4
        assert result.shortlisted == 1
        assert result.discarded == 1
        assert result.duplicates == 1
        assert result.skipped_timeout == 1
        assert result.errors == 0

        # Verify DuckDB summary
        stats = await repo.get_stats()
        # total unique rows: existing (1) + match (1) + mismatch (1) + slow (1) = 4
        assert stats["total"] == 4
        assert stats.get("SHORTLISTED", 0) == 2  # existing + new match
        assert stats.get("DISCARDED", 0) == 1
        assert stats.get("SKIPPED_TIMEOUT", 0) == 1

        await repo.close()


class TestTier4CliModes:
    """Validate Typer CLI headless and terminal execution modes."""

    def test_cli_run_mock_headless(self, tmp_path: Path):
        db_path = str(tmp_path / "cli_headless.duckdb")
        result = runner.invoke(app, [
            "run",
            "--mock",
            "--headless",
            "--limit", "3",
            "--db", db_path,
        ])
        assert result.exit_code == 0
        assert "autonomous execution summary" in result.stdout or "Execution Completed" in result.stdout

    def test_cli_run_mock_default_terminal_mode(self, tmp_path: Path):
        db_path = str(tmp_path / "cli_terminal.duckdb")
        result = runner.invoke(app, [
            "run",
            "--mock",
            "--no-headless",
            "--limit", "2",
            "--db", db_path,
        ])
        assert result.exit_code == 0
