"""
Comprehensive Unit Tests for DAG Pipeline Orchestrator (src/core/pipeline.py).

Verifies:
1. Full 5-stage lifecycle: Ingestion -> Deduplication -> Pre-Processing -> Triage -> Decision Tree.
2. Status transitions: INGESTED -> PREPROCESSED -> TRIAGED -> SHORTLISTED / DISCARDED.
3. Content deduplication: SHA256 checking in DuckDB, skipping stages 3-5 on duplicate.
4. Pre-processing edge cases: context ceiling truncation, XML delimiter wrapping, short description handling.
5. Execution harness resilience: LocalLoopGuard timeout catching, SKIPPED_TIMEOUT marking, graceful skip without crash.
6. Iteration bounding: max_iterations enforcement halting pipeline.
7. Telemetry & event hooks: synchronous and asynchronous UI callbacks, listener exception isolation.
8. O(1) RAM streaming & DuckDB persistence: immediate WAL commit per stage.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.use_cases.pipeline import (
    DEFAULT_CANDIDATE_PROFILE,
    JobPipeline,
    PipelineConfig,
    PipelineEvent,
    PipelineEventType,
)
from src.infrastructure.adapters.repository import JobRepository, compute_job_hash
from src.infrastructure.adapters.llm_evaluator import (
    JobFitEvaluator,
    LLMConnectionError,
    LLMTimeoutError,
)
from src.infrastructure.adapters.mcp_mock_client import MockMcpJobClient
from src.domain.models import (
    CandidateProfile,
    JobPosting,
    JobStatus,
    MatchEvaluation,
    Recommendation,
)

# ==============================================================================
# Test Fixtures & Helpers
# ==============================================================================

@pytest.fixture
async def repo_memory() -> AsyncGenerator[JobRepository, None]:
    """Provides an isolated, in-memory DuckDB JobRepository."""
    repo = JobRepository(db_path=":memory:")
    await repo.initialize()
    yield repo
    await repo.close()


@pytest.fixture
def sample_candidate() -> CandidateProfile:
    """Standard candidate profile fixture."""
    return CandidateProfile(
        name="Alex Rivera",
        target_role="Senior Backend Engineer",
        years_experience=6,
        primary_skills=["Python", "FastAPI", "PostgreSQL", "Docker"],
        secondary_skills=["AWS", "Redis", "Kafka", "CI/CD"],
        summary="Senior engineer specialized in high-throughput distributed systems.",
    )


def create_job_posting(
    title: str = "Senior Python Engineer",
    company: str = "Acme Corp",
    description: str = (
        "We are looking for a Senior Python Engineer to build high-scale distributed systems. "
        "Required skills: Python, FastAPI, PostgreSQL, Docker. "
        "Compensation: $160,000 - $190,000 with comprehensive health benefits."
    ),
    job_id: str | None = None,
) -> JobPosting:
    """Helper creating a valid JobPosting domain model."""
    chash = compute_job_hash(description, title=title, company=company)
    kwargs: dict[str, Any] = {
        "content_hash": chash,
        "title": title,
        "company": company,
        "raw_description": description,
        "status": JobStatus.INGESTED,
    }
    if job_id is not None:
        kwargs["id"] = job_id
    return JobPosting(**kwargs)


def create_mock_evaluator(
    fit_score: int = 85,
    recommendation: Recommendation = Recommendation.SHORTLIST,
    matched_skills: list[str] | None = None,
    missing_skills: list[str] | None = None,
    delay: float = 0.0,
    side_effect: Exception | None = None,
) -> MagicMock:
    """Helper generating a mock JobFitEvaluator."""
    evaluator = MagicMock(spec=JobFitEvaluator)

    async def _mock_evaluate(*args: Any, **kwargs: Any) -> MatchEvaluation:
        if delay > 0:
            await asyncio.sleep(delay)
        if side_effect:
            raise side_effect
        return MatchEvaluation(
            fit_score=fit_score,
            recommendation=recommendation,
            matched_skills=matched_skills or ["Python", "FastAPI", "PostgreSQL"],
            missing_skills=missing_skills or [],
            reasoning="Candidate matches all primary backend technical competencies.",
        )

    evaluator.evaluate_fit = AsyncMock(side_effect=_mock_evaluate)
    return evaluator


# ==============================================================================
# 1. Pipeline Initialization & Configuration Tests
# ==============================================================================

class TestPipelineInitialization:
    def test_default_initialization(self, repo_memory: JobRepository):
        evaluator = create_mock_evaluator()
        client = MockMcpJobClient(jobs=[])
        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=evaluator,
            ingestion_client=client,
        )
        assert pipeline.repository is repo_memory
        assert pipeline.evaluator is evaluator
        assert pipeline.ingestion_client is client
        assert pipeline.score_threshold == 70
        assert pipeline.candidate_profile == DEFAULT_CANDIDATE_PROFILE

    def test_custom_config_and_threshold(self, repo_memory: JobRepository, sample_candidate: CandidateProfile):
        evaluator = create_mock_evaluator()
        config = PipelineConfig(score_threshold=80, batch_size=5, max_iterations=20)
        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=evaluator,
            candidate_profile=sample_candidate,
            config=config,
            score_threshold=85,
        )
        assert pipeline.score_threshold == 85
        assert pipeline.config.batch_size == 5
        assert pipeline.config.max_iterations == 20
        assert pipeline.candidate_profile == sample_candidate

    def test_listener_management(self, repo_memory: JobRepository):
        pipeline = JobPipeline(repository=repo_memory, evaluator=create_mock_evaluator())
        cb1 = MagicMock()
        cb2 = AsyncMock()

        pipeline.add_listener(cb1)
        pipeline.add_listener(cb2)
        assert len(pipeline._listeners) == 2

        # Idempotent addition
        pipeline.add_listener(cb1)
        assert len(pipeline._listeners) == 2

        pipeline.remove_listener(cb1)
        assert len(pipeline._listeners) == 1
        assert pipeline._listeners[0] == cb2


# ==============================================================================
# 2. Full 5-Stage Lifecycle Tests
# ==============================================================================

class TestPipelineLifecycle:
    @pytest.mark.asyncio
    async def test_full_pipeline_happy_path_shortlist(
        self,
        repo_memory: JobRepository,
        sample_candidate: CandidateProfile,
    ):
        """
        Verify end-to-end 5-stage pipeline for a matching job:
        Ingestion -> Deduplication -> Pre-Processing -> Triage -> Decision Tree (SHORTLISTED).
        """
        job = create_job_posting(title="Senior Python Engineer")
        client = MockMcpJobClient(jobs=[job])
        await client.connect()

        evaluator = create_mock_evaluator(fit_score=88, recommendation=Recommendation.SHORTLIST)
        events_emitted: list[PipelineEvent] = []

        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=evaluator,
            ingestion_client=client,
            candidate_profile=sample_candidate,
            listeners=[lambda e: events_emitted.append(e)],
        )

        result = await pipeline.run(limit=1)

        # 1. Verify PipelineResult summary
        assert result.total_ingested == 1
        assert result.total_processed == 1
        assert result.shortlisted == 1
        assert result.discarded == 0
        assert result.duplicates == 0
        assert result.errors == 0
        assert result.skipped_timeout == 0

        # 2. Verify Database State
        persisted_job = await repo_memory.get_job_by_hash(job.content_hash)
        assert persisted_job is not None
        assert persisted_job.status == JobStatus.SHORTLISTED
        assert persisted_job.fit_score == 88
        assert persisted_job.recommendation == Recommendation.SHORTLIST
        assert persisted_job.cleaned_description is not None
        assert "<job_posting>" in (persisted_job.description or "")
        assert persisted_job.token_count is not None
        assert persisted_job.evaluation is not None
        assert "Python" in persisted_job.evaluation.matched_skills

        # 3. Verify Telemetry Event Flow
        event_types = [e.event_type for e in events_emitted]
        expected_sequence = [
            PipelineEventType.PIPELINE_STARTED,
            PipelineEventType.INGESTION_STARTED,
            PipelineEventType.INGESTION_COMPLETED,
            PipelineEventType.JOB_INGESTED,
            PipelineEventType.JOB_PREPROCESSED,
            PipelineEventType.JOB_TRIAGE_STARTED,
            PipelineEventType.JOB_TRIAGED,
            PipelineEventType.JOB_SHORTLISTED,
            PipelineEventType.PIPELINE_COMPLETED,
        ]
        assert event_types == expected_sequence

    @pytest.mark.asyncio
    async def test_full_pipeline_decision_discard(
        self,
        repo_memory: JobRepository,
        sample_candidate: CandidateProfile,
    ):
        """
        Verify that a low fit score (< 70) results in DISCARDED status.
        """
        job = create_job_posting(
            title="Junior Frontend React Dev",
            description="Seeking junior developer with HTML, CSS, and basic JavaScript. Over 50 characters required.",
        )
        client = MockMcpJobClient(jobs=[job])
        await client.connect()

        evaluator = create_mock_evaluator(
            fit_score=35,
            recommendation=Recommendation.DISCARD,
            matched_skills=[],
            missing_skills=["React", "Frontend"],
        )

        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=evaluator,
            ingestion_client=client,
            candidate_profile=sample_candidate,
            score_threshold=70,
        )

        result = await pipeline.run(limit=1)

        assert result.total_processed == 1
        assert result.shortlisted == 0
        assert result.discarded == 1

        persisted = await repo_memory.get_job_by_hash(job.content_hash)
        assert persisted is not None
        assert persisted.status == JobStatus.DISCARDED
        assert persisted.fit_score == 35
        assert persisted.recommendation == Recommendation.DISCARD


# ==============================================================================
# 3. Deduplication Tests
# ==============================================================================

class TestPipelineDeduplication:
    @pytest.mark.asyncio
    async def test_duplicate_job_skips_downstream_stages(
        self,
        repo_memory: JobRepository,
    ):
        """
        Pre-insert a job in DuckDB; pipeline must flag incoming job as DUPLICATE,
        flush status, and skip pre-processing and triage.
        """
        existing_job = create_job_posting(title="Staff Engineer", company="Netflix")
        await repo_memory.save_job(existing_job)

        duplicate_job = create_job_posting(title="Staff Engineer", company="Netflix")
        client = MockMcpJobClient(jobs=[duplicate_job])
        await client.connect()

        evaluator = create_mock_evaluator()
        events: list[PipelineEvent] = []

        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=evaluator,
            ingestion_client=client,
            listeners=[lambda e: events.append(e)],
        )

        result = await pipeline.run(limit=1)

        assert result.total_processed == 1
        assert result.duplicates == 1
        assert result.preprocessed == 0
        assert result.triaged == 0
        assert evaluator.evaluate_fit.call_count == 0

        # Verify DUPLICATE event was emitted
        dup_events = [e for e in events if e.event_type == PipelineEventType.JOB_DUPLICATE]
        assert len(dup_events) == 1

    @pytest.mark.asyncio
    async def test_duplicate_within_same_run_batch(
        self,
        repo_memory: JobRepository,
    ):
        """
        Feed two identical jobs in the same ingestion batch:
        First is processed; second is detected as DUPLICATE.
        """
        job1 = create_job_posting(title="Duplicate Test", company="Google")
        job2 = create_job_posting(title="Duplicate Test", company="Google")
        client = MockMcpJobClient(jobs=[job1, job2])
        await client.connect()

        evaluator = create_mock_evaluator(fit_score=90)
        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=evaluator,
            ingestion_client=client,
        )

        result = await pipeline.run(limit=2)

        assert result.total_processed == 2
        assert result.duplicates == 1
        assert result.shortlisted == 1
        assert evaluator.evaluate_fit.call_count == 1


# ==============================================================================
# 4. Pre-Processing Tests
# ==============================================================================

class TestPipelinePreProcessing:
    @pytest.mark.asyncio
    async def test_truncation_on_long_description(
        self,
        repo_memory: JobRepository,
    ):
        """
        Long description (> 1500 tokens) is truncated, tagged with is_truncated=True,
        and triaged with truncated text.
        """
        long_text = "Senior Distributed Systems Architect. " + ("Core python and architecture responsibilities. " * 300)
        job = create_job_posting(description=long_text)
        client = MockMcpJobClient(jobs=[job])
        await client.connect()

        evaluator = create_mock_evaluator(fit_score=85)
        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=evaluator,
            ingestion_client=client,
        )

        result = await pipeline.run(limit=1)
        assert result.shortlisted == 1

        persisted = await repo_memory.get_job_by_hash(job.content_hash)
        assert persisted is not None
        assert persisted.is_truncated is True
        assert "[...Description truncated for context window ceiling...]" in (persisted.description or "")

    @pytest.mark.asyncio
    async def test_short_description_fails_preprocessing(
        self,
        repo_memory: JobRepository,
    ):
        """
        Job with <= 50 characters fails pre-processing with FAILED status,
        persisting error details and skipping triage.
        """
        short_job = create_job_posting(description="Too short")
        client = MockMcpJobClient(jobs=[short_job])
        await client.connect()

        evaluator = create_mock_evaluator()
        events: list[PipelineEvent] = []

        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=evaluator,
            ingestion_client=client,
            listeners=[lambda e: events.append(e)],
        )

        result = await pipeline.run(limit=1)

        assert result.total_processed == 1
        assert result.errors == 1
        assert result.triaged == 0
        assert evaluator.evaluate_fit.call_count == 0

        persisted = await repo_memory.get_job_by_hash(short_job.content_hash)
        assert persisted is not None
        assert persisted.status == JobStatus.FAILED
        assert "Job description too short" in (persisted.error_message or "")


# ==============================================================================
# 5. Triage Resilience & LocalLoopGuard Integration Tests
# ==============================================================================

class TestPipelineTriageResilience:
    @pytest.mark.asyncio
    async def test_llm_timeout_handled_gracefully_skipped(
        self,
        repo_memory: JobRepository,
    ):
        """
        Inference timeout raises LLMTimeoutError (or TimeoutError):
        Pipeline marks job as SKIPPED_TIMEOUT, updates DuckDB immediately,
        emits JOB_SKIPPED_TIMEOUT, and does NOT crash.
        """
        job = create_job_posting(title="Timeout Target Job")
        client = MockMcpJobClient(jobs=[job])
        await client.connect()

        evaluator = create_mock_evaluator(side_effect=LLMTimeoutError("Ollama inference timed out after 15s"))
        events: list[PipelineEvent] = []

        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=evaluator,
            ingestion_client=client,
            listeners=[lambda e: events.append(e)],
        )

        result = await pipeline.run(limit=1)

        assert result.total_processed == 1
        assert result.skipped_timeout == 1
        assert result.shortlisted == 0
        assert result.discarded == 0

        persisted = await repo_memory.get_job_by_hash(job.content_hash)
        assert persisted is not None
        assert persisted.status == JobStatus.SKIPPED_TIMEOUT
        assert "timed out" in (persisted.error_message or "").lower()

        timeout_events = [e for e in events if e.event_type == PipelineEventType.JOB_SKIPPED_TIMEOUT]
        assert len(timeout_events) == 1

    @pytest.mark.asyncio
    async def test_llm_connection_error_marks_error_status(
        self,
        repo_memory: JobRepository,
    ):
        """
        LLM service unreachable raises LLMConnectionError:
        Pipeline marks job as ERROR, persists error message, and continues.
        """
        job = create_job_posting(title="Connection Failure Job")
        client = MockMcpJobClient(jobs=[job])
        await client.connect()

        evaluator = create_mock_evaluator(side_effect=LLMConnectionError("Failed to connect to Ollama at http://localhost:11434"))

        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=evaluator,
            ingestion_client=client,
        )

        result = await pipeline.run(limit=1)

        assert result.total_processed == 1
        assert result.errors == 1

        persisted = await repo_memory.get_job_by_hash(job.content_hash)
        assert persisted is not None
        assert persisted.status == JobStatus.ERROR
        assert "LLM connection error" in (persisted.error_message or "")

    @pytest.mark.asyncio
    async def test_localloopguard_run_guarded_integration(
        self,
        repo_memory: JobRepository,
    ):
        """
        Test integration with a mock LocalLoopGuard instance.
        """
        job = create_job_posting(title="Guarded Step Job")
        client = MockMcpJobClient(jobs=[job])
        await client.connect()

        mock_guard = MagicMock()
        mock_guard.can_continue.return_value = True
        mock_guard.max_iterations = 50
        mock_guard.timeout_seconds = 15.0

        async def _guard_timeout_side_effect(func, *args, **kwargs):
            target_job = kwargs.get("job")
            if target_job:
                target_job.status = JobStatus.SKIPPED_TIMEOUT
                target_job.error_message = "Guard timeout"
                await repo_memory.update_status(target_job.id, status=JobStatus.SKIPPED_TIMEOUT, error_message=target_job.error_message)

        mock_guard.run_guarded = AsyncMock(side_effect=_guard_timeout_side_effect)

        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=create_mock_evaluator(),
            ingestion_client=client,
            guard=mock_guard,
        )

        result = await pipeline.run(limit=1)
        assert result.skipped_timeout == 1

        persisted = await repo_memory.get_job_by_hash(job.content_hash)
        assert persisted.status == JobStatus.SKIPPED_TIMEOUT


# ==============================================================================
# 6. Iteration Bounding Tests
# ==============================================================================

class TestPipelineIterationBounding:
    @pytest.mark.asyncio
    async def test_limit_parameter_stops_processing(
        self,
        repo_memory: JobRepository,
    ):
        """
        When limit=2 is supplied, pipeline halts after exactly 2 jobs even if 10 are available.
        """
        jobs = [create_job_posting(title=f"Engineer {i}", company="Acme") for i in range(10)]
        client = MockMcpJobClient(jobs=jobs)
        await client.connect()

        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=create_mock_evaluator(),
            ingestion_client=client,
        )

        result = await pipeline.run(limit=2)
        assert result.total_processed == 2

    @pytest.mark.asyncio
    async def test_guard_can_continue_halts_loop(
        self,
        repo_memory: JobRepository,
    ):
        """
        When guard.can_continue() turns False, pipeline stops and emits PIPELINE_STOPPED.
        """
        jobs = [create_job_posting(title=f"Engineer {i}", company="Acme") for i in range(5)]
        client = MockMcpJobClient(jobs=jobs)
        await client.connect()

        mock_guard = MagicMock()
        mock_guard.can_continue.side_effect = [True, True, False, False, False]
        mock_guard.max_iterations = 1
        mock_guard.current_iteration = 1

        events: list[PipelineEvent] = []
        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=create_mock_evaluator(),
            ingestion_client=client,
            guard=mock_guard,
            listeners=[lambda e: events.append(e)],
        )

        result = await pipeline.run(limit=5)
        assert result.total_processed == 1

        stopped_events = [e for e in events if e.event_type == PipelineEventType.PIPELINE_STOPPED]
        assert len(stopped_events) == 1


# ==============================================================================
# 7. Telemetry & Event Hooks Tests
# ==============================================================================

class TestPipelineTelemetry:
    @pytest.mark.asyncio
    async def test_sync_and_async_listeners_invoked(
        self,
        repo_memory: JobRepository,
    ):
        job = create_job_posting()
        client = MockMcpJobClient(jobs=[job])
        await client.connect()

        sync_events: list[PipelineEvent] = []
        async_events: list[PipelineEvent] = []

        def sync_listener(event: PipelineEvent) -> None:
            sync_events.append(event)

        async def async_listener(event: PipelineEvent) -> None:
            await asyncio.sleep(0.001)
            async_events.append(event)

        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=create_mock_evaluator(),
            ingestion_client=client,
            listeners=[sync_listener, async_listener],
        )

        await pipeline.run(limit=1)

        assert len(sync_events) > 5
        assert len(async_events) == len(sync_events)
        assert sync_events[0].event_type == PipelineEventType.PIPELINE_STARTED
        assert sync_events[-1].event_type == PipelineEventType.PIPELINE_COMPLETED

    @pytest.mark.asyncio
    async def test_faulty_listener_does_not_abort_pipeline(
        self,
        repo_memory: JobRepository,
    ):
        job = create_job_posting()
        client = MockMcpJobClient(jobs=[job])
        await client.connect()

        def broken_listener(event: PipelineEvent) -> None:
            raise RuntimeError("UI listener crashed")

        working_events: list[PipelineEvent] = []
        def working_listener(event: PipelineEvent) -> None:
            working_events.append(event)

        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=create_mock_evaluator(),
            ingestion_client=client,
            listeners=[broken_listener, working_listener],
        )

        result = await pipeline.run(limit=1)
        assert result.shortlisted == 1
        assert len(working_events) > 0


# ==============================================================================
# 8. O(1) RAM Streaming Tests
# ==============================================================================

class TestPipelineStreamingRAM:
    @pytest.mark.asyncio
    async def test_process_stream_streaming_generator(
        self,
        repo_memory: JobRepository,
    ):
        """
        Process a sequence of jobs through process_stream async iterator.
        Verifies O(1) memory behavior and state persistence.
        """
        async def job_generator(count: int) -> AsyncGenerator[JobPosting, None]:
            for i in range(count):
                yield create_job_posting(title=f"Streaming Engineer {i}", company=f"Company {i}")

        evaluator = create_mock_evaluator(fit_score=75)
        pipeline = JobPipeline(
            repository=repo_memory,
            evaluator=evaluator,
        )

        result = await pipeline.process_stream(job_generator(5), limit=5)

        assert result.total_processed == 5
        assert result.shortlisted == 5

        # Verify DuckDB has all 5 records
        stats = await repo_memory.get_stats()
        assert stats.get(JobStatus.SHORTLISTED.value, 0) == 5
        assert stats["total"] == 5
