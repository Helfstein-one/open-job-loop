"""
DAG Pipeline Orchestrator for open-job-loop.

Executes a linear 5-stage directed acyclic graph (DAG) data pipeline:
1. Ingestion: Fetches job postings via BaseJobIngestionClient (McpJobClient / MockMcpJobClient).
2. Deduplication: Evaluates SHA256 content_hash against DuckDB; flags DUPLICATE and halts downstream stages.
3. Pre-Processing: Cleans boilerplate, bounds context ceiling via TextTruncator, and wraps in XML delimiters.
4. Triage: Evaluates candidate match via JobFitEvaluator (local Llama 3.2 via Instructor), guarded by LocalLoopGuard.
5. Decision Tree: Determines SHORTLISTED vs DISCARDED based on fit_score threshold; flushes immediately to DuckDB.

Guarantees:
- O(1) RAM streaming: stateful entities are flushed and committed immediately to DuckDB; no unbounded collections in memory.
- Fault tolerance: LocalLoopGuard wall-clock timeouts gracefully skip jobs (SKIPPED_TIMEOUT) without crashing.
- UI telemetry: Observable lifecycle events emitted via sync/async callback hooks and optional UI listener adapter.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import (
    Any,
)

from src.core.harness import (
    CircuitOpenError,
    LocalLoopGuard,
)
from src.core.truncator import DescriptionTooShortError, TextTruncator
from src.db.repository import JobRepository, compute_job_hash
from src.llm.evaluator import (
    JobFitEvaluator,
    LLMConnectionError,
    LLMTimeoutError,
)
from src.mcp.client import BaseJobIngestionClient
from src.models.schemas import (
    CandidateProfile,
    JobPosting,
    JobStatus,
    Recommendation,
)

logger = logging.getLogger(__name__)


# ==============================================================================
# Pipeline Event System
# ==============================================================================

class PipelineEventType(str, Enum):
    """
    Observable pipeline telemetry event types emitted during DAG execution.
    """
    PIPELINE_STARTED = "PIPELINE_STARTED"
    INGESTION_STARTED = "INGESTION_STARTED"
    INGESTION_COMPLETED = "INGESTION_COMPLETED"
    JOB_INGESTED = "JOB_INGESTED"
    JOB_DUPLICATE = "JOB_DUPLICATE"
    JOB_PREPROCESSED = "JOB_PREPROCESSED"
    JOB_TRIAGE_STARTED = "JOB_TRIAGE_STARTED"
    JOB_TRIAGED = "JOB_TRIAGED"
    JOB_SHORTLISTED = "JOB_SHORTLISTED"
    JOB_DISCARDED = "JOB_DISCARDED"
    JOB_SKIPPED_TIMEOUT = "JOB_SKIPPED_TIMEOUT"
    JOB_ERROR = "JOB_ERROR"
    PIPELINE_STOPPED = "PIPELINE_STOPPED"
    PIPELINE_COMPLETED = "PIPELINE_COMPLETED"


@dataclass
class PipelineEvent:
    """
    Telemetry event payload delivered to UI and monitoring subscribers.
    """
    event_type: PipelineEventType
    job: JobPosting | None = None
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    stage_number: int | None = None


PipelineCallback = Callable[[PipelineEvent], Awaitable[None] | None]


# ==============================================================================
# Configuration & Result Schemas
# ==============================================================================

@dataclass
class PipelineConfig:
    """
    Operational configuration for JobPipeline execution.
    """
    score_threshold: int = 70
    batch_size: int = 10
    max_iterations: int = 50
    timeout_seconds: float = 15.0
    auto_wrap_xml: bool = True
    stop_on_llm_connection_error: bool = False


@dataclass
class PipelineResult:
    """
    Aggregated telemetry summary returned upon pipeline completion.
    Guarantees O(1) RAM consumption by storing only scalar counters.
    Supports dictionary access and singular/plural metric aliases.
    """
    total_ingested: int = 0
    duplicates: int = 0
    preprocessed: int = 0
    triaged: int = 0
    shortlisted: int = 0
    discarded: int = 0
    skipped_timeout: int = 0
    errors: int = 0
    total_processed: int = 0
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert result summary into dictionary with singular/plural aliases."""
        d = asdict(self)
        d["duplicate"] = self.duplicates
        d["error"] = self.errors
        d["ingested"] = self.total_ingested
        d["discovered"] = self.total_ingested
        return d

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def keys(self) -> Any:
        return self.to_dict().keys()

    def values(self) -> Any:
        return self.to_dict().values()

    def items(self) -> Any:
        return self.to_dict().items()


DEFAULT_CANDIDATE_PROFILE = CandidateProfile(
    name="Candidate",
    target_role="Software Engineer",
    years_experience=5,
    primary_skills=["Python", "FastAPI", "SQL", "Docker"],
    secondary_skills=["Git", "Linux", "CI/CD", "Testing"],
    summary="Full-stack / backend software engineer with Python expertise.",
)


# ==============================================================================
# Job Pipeline Orchestrator
# ==============================================================================

class JobPipeline:
    """
    Linear 5-Stage Directed Acyclic Graph (DAG) Pipeline Orchestrator.

    Lifecycle stages:
    1. Ingestion (`stage_1_ingest`)
    2. Deduplication (`stage_2_deduplicate`)
    3. Pre-Processing (`stage_3_preprocess`)
    4. Triage (`stage_4_triage`)
    5. Decision Tree (`stage_5_decision_tree`)

    Features:
    - Bounded O(1) RAM streaming via immediate DuckDB writes & checkpoints.
    - LocalLoopGuard iteration limits and wall-clock timeout protection.
    - Synchronous and asynchronous UI event telemetry hooks.
    """

    def __init__(
        self,
        repository: JobRepository,
        evaluator: JobFitEvaluator,
        ingestion_client: BaseJobIngestionClient | None = None,
        candidate_profile: CandidateProfile | str | dict[str, Any] | None = None,
        truncator: TextTruncator | None = None,
        guard: LocalLoopGuard | None = None,
        config: PipelineConfig | None = None,
        score_threshold: int | None = None,
        listeners: list[PipelineCallback] | None = None,
        mcp_client: BaseJobIngestionClient | None = None,
        ui_listener: Any | None = None,
    ) -> None:
        """
        Initialize the JobPipeline orchestrator.

        Args:
            repository: Initialized DuckDB JobRepository instance.
            evaluator: JobFitEvaluator instance for LLM inference.
            ingestion_client: Optional BaseJobIngestionClient (McpJobClient / MockMcpJobClient).
            candidate_profile: Target candidate profile (CandidateProfile, string, or dict).
            truncator: Optional TextTruncator instance.
            guard: Optional LocalLoopGuard instance for iteration & timeout bounding.
            config: Optional PipelineConfig instance.
            score_threshold: Optional override for shortlist threshold (defaults to config or 70).
            listeners: Optional list of event telemetry callbacks.
            mcp_client: Alias for ingestion_client.
            ui_listener: Optional UI manager (e.g. BasePipelineUI) to receive telemetry.
        """
        self.repository = repository
        self.evaluator = evaluator
        self.ingestion_client = ingestion_client or mcp_client
        self.guard = guard

        self.config = config or PipelineConfig()
        if score_threshold is not None:
            self.config.score_threshold = score_threshold

        self.truncator = truncator or TextTruncator(
            max_tokens=1500,
            min_chars=50,
            wrap_xml=self.config.auto_wrap_xml,
        )

        # Normalize candidate profile
        if candidate_profile is None:
            self.candidate_profile: CandidateProfile | str = DEFAULT_CANDIDATE_PROFILE
        elif isinstance(candidate_profile, dict):
            self.candidate_profile = CandidateProfile(**candidate_profile)
        else:
            self.candidate_profile = candidate_profile

        self._listeners: list[PipelineCallback] = list(listeners or [])
        self._current_metrics: dict[str, int] = {
            "discovered": 0,
            "ingested": 0,
            "duplicate": 0,
            "preprocessed": 0,
            "shortlisted": 0,
            "discarded": 0,
            "skipped_timeout": 0,
            "error": 0,
        }

        if ui_listener is not None:
            self._setup_ui_adapter(ui_listener)

    def _setup_ui_adapter(self, ui: Any) -> None:
        """Connect UI manager to pipeline events."""
        def _ui_adapter(event: PipelineEvent) -> None:
            try:
                ev = event.event_type
                if ev == PipelineEventType.INGESTION_STARTED:
                    if hasattr(ui, "on_stage_update"):
                        ui.on_stage_update(1, "1. Ingestion (MCP)", "RUNNING", event.message)
                elif ev == PipelineEventType.INGESTION_COMPLETED:
                    if hasattr(ui, "on_stage_update"):
                        ui.on_stage_update(1, "1. Ingestion (MCP)", "DONE", event.message)
                    if hasattr(ui, "state") and "count" in event.data:
                        self._current_metrics["discovered"] = event.data["count"]
                elif ev == PipelineEventType.JOB_INGESTED:
                    self._current_metrics["ingested"] += 1
                    if hasattr(ui, "on_stage_update"):
                        ui.on_stage_update(2, "2. Deduplication (DuckDB)", "DONE")
                    if hasattr(ui, "on_job_start") and event.job is not None:
                        ui.on_job_start(event.job, iteration=self._current_metrics["ingested"], max_iterations=self.config.max_iterations)
                elif ev == PipelineEventType.JOB_DUPLICATE:
                    self._current_metrics["duplicate"] += 1
                    if hasattr(ui, "on_stage_update"):
                        ui.on_stage_update(2, "2. Deduplication (DuckDB)", "SKIPPED", event.message)
                    if hasattr(ui, "on_job_skipped"):
                        ui.on_job_skipped("Duplicate posting", job=event.job)
                elif ev == PipelineEventType.JOB_PREPROCESSED:
                    self._current_metrics["preprocessed"] += 1
                    if hasattr(ui, "on_stage_update"):
                        ui.on_stage_update(3, "3. Pre-Processing (Truncator)", "DONE")
                elif ev == PipelineEventType.JOB_TRIAGE_STARTED:
                    if hasattr(ui, "on_stage_update"):
                        ui.on_stage_update(4, "4. Triage (Llama 3.2)", "RUNNING")
                elif ev == PipelineEventType.JOB_TRIAGED:
                    if hasattr(ui, "on_stage_update"):
                        ui.on_stage_update(4, "4. Triage (Llama 3.2)", "DONE")
                    if hasattr(ui, "on_job_triaged") and event.job is not None and getattr(event.job, "evaluation", None):
                        ui.on_job_triaged(event.job, event.job.evaluation)
                elif ev == PipelineEventType.JOB_SHORTLISTED:
                    self._current_metrics["shortlisted"] += 1
                    if hasattr(ui, "on_stage_update"):
                        ui.on_stage_update(5, "5. Decision Tree (Flush)", "DONE")
                    if hasattr(ui, "on_job_completed") and event.job is not None:
                        ui.on_job_completed(event.job, self._current_metrics)
                elif ev == PipelineEventType.JOB_DISCARDED:
                    self._current_metrics["discarded"] += 1
                    if hasattr(ui, "on_stage_update"):
                        ui.on_stage_update(5, "5. Decision Tree (Flush)", "DONE")
                    if hasattr(ui, "on_job_completed") and event.job is not None:
                        ui.on_job_completed(event.job, self._current_metrics)
                elif ev == PipelineEventType.JOB_SKIPPED_TIMEOUT:
                    self._current_metrics["skipped_timeout"] += 1
                    if hasattr(ui, "on_stage_update"):
                        ui.on_stage_update(4, "4. Triage (Llama 3.2)", "TIMEOUT", event.message)
                    if hasattr(ui, "on_job_skipped"):
                        ui.on_job_skipped("LLM inference timeout", job=event.job)
                elif ev == PipelineEventType.JOB_ERROR:
                    self._current_metrics["error"] += 1
                    stage = event.stage_number or 4
                    if hasattr(ui, "on_stage_update"):
                        ui.on_stage_update(stage, f"{stage}. Stage Error", "ERROR", event.message)
                    if hasattr(ui, "on_job_skipped"):
                        ui.on_job_skipped("Job processing error", job=event.job)
                if hasattr(ui, "on_metrics_update"):
                    ui.on_metrics_update(self._current_metrics)
            except Exception as e:  # noqa: BLE001
                logger.debug("Error in UI adapter callback: %s", e)

        self.add_listener(_ui_adapter)

    @property
    def score_threshold(self) -> int:
        """Configured fit score threshold for shortlist recommendation."""
        return self.config.score_threshold

    @score_threshold.setter
    def score_threshold(self, value: int) -> None:
        self.config.score_threshold = value

    # --------------------------------------------------------------------------
    # Telemetry & Event Subscription
    # --------------------------------------------------------------------------

    def add_listener(self, callback: PipelineCallback) -> None:
        """
        Subscribe a callback function to receive pipeline telemetry events.
        """
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: PipelineCallback) -> None:
        """
        Unsubscribe a callback function from pipeline telemetry events.
        """
        if callback in self._listeners:
            self._listeners.remove(callback)

    async def emit(self, event: PipelineEvent) -> None:
        """
        Emit a telemetry event to all registered listeners.
        Exceptions raised by individual listeners are caught and logged to prevent pipeline interruption.
        """
        for listener in self._listeners:
            try:
                if inspect.iscoroutinefunction(listener):
                    await listener(event)
                else:
                    call_res = listener(event)
                    if inspect.iscoroutine(call_res):
                        await call_res
            except Exception as exc:  # noqa: BLE001
                logger.warning("Pipeline event listener error on %s: %s", event.event_type, exc)

    # --------------------------------------------------------------------------
    # Stage 1: Ingestion
    # --------------------------------------------------------------------------

    async def stage_1_ingest(self, limit: int = 10) -> list[JobPosting]:
        """
        Stage 1: Ingest raw job postings from the configured MCP ingestion client.

        Args:
            limit: Maximum number of jobs to fetch.

        Returns:
            List of normalized JobPosting entities.

        Raises:
            ValueError: If no ingestion_client was provided.
            CircuitOpenError: If LocalLoopGuard circuit breaker trips and blocks fetch.
        """
        if self.ingestion_client is None:
            raise ValueError("Cannot ingest jobs: no ingestion_client provided to JobPipeline")

        await self.emit(PipelineEvent(
            event_type=PipelineEventType.INGESTION_STARTED,
            message=f"Fetching up to {limit} jobs from ingestion client",
            stage_number=1,
            data={"limit": limit},
        ))

        # Check guard circuit breaker if present
        if (
            self.guard is not None
            and getattr(self.guard, "circuit_breaker", None) is not None
            and hasattr(self.guard.circuit_breaker, "allow_request")
            and not self.guard.circuit_breaker.allow_request()
        ):
            raise CircuitOpenError("MCP Circuit Breaker is OPEN.")

        jobs = await self.ingestion_client.fetch_jobs(limit=limit)

        await self.emit(PipelineEvent(
            event_type=PipelineEventType.INGESTION_COMPLETED,
            message=f"Ingested {len(jobs)} jobs",
            stage_number=1,
            data={"count": len(jobs)},
        ))

        return jobs

    # --------------------------------------------------------------------------
    # Stage 2: Deduplication
    # --------------------------------------------------------------------------

    async def stage_2_deduplicate(self, job: JobPosting) -> bool:
        """
        Stage 2: Deduplicate job posting by canonical SHA256 content hash in DuckDB.

        If already present in DuckDB:
        - Updates job.status = JobStatus.DUPLICATE
        - Emits JOB_DUPLICATE event
        - Returns False (skips downstream stages)

        If unique:
        - Sets job.status = JobStatus.INGESTED
        - Saves atomically to DuckDB (`save_job`)
        - Emits JOB_INGESTED event
        - Returns True (proceeds to Stage 3)

        Args:
            job: Target JobPosting entity.

        Returns:
            True if job is unique and persisted, False if duplicate.
        """
        if not job.content_hash:
            job.content_hash = compute_job_hash(
                raw_description=job.raw_description,
                title=job.title,
                company=job.company,
            )

        # 1. Probe database for existing content hash
        is_dup = await self.repository.is_duplicate(job.content_hash)
        if is_dup:
            job.status = JobStatus.DUPLICATE
            await self.emit(PipelineEvent(
                event_type=PipelineEventType.JOB_DUPLICATE,
                job=job,
                stage_number=2,
                message=f"Job '{job.title}' at '{job.company}' is a duplicate (hash {job.content_hash[:8]}...)",
            ))
            return False

        # 2. Persist new job atomically to DuckDB
        job.status = JobStatus.INGESTED
        saved = await self.repository.save_job(job)
        if not saved:
            # Atomic conflict on concurrent insertion
            job.status = JobStatus.DUPLICATE
            await self.emit(PipelineEvent(
                event_type=PipelineEventType.JOB_DUPLICATE,
                job=job,
                stage_number=2,
                message=f"Job '{job.title}' collided on insert (duplicate hash {job.content_hash[:8]}...)",
            ))
            return False

        await self.emit(PipelineEvent(
            event_type=PipelineEventType.JOB_INGESTED,
            job=job,
            stage_number=2,
            message=f"Job '{job.title}' ingested and persisted (hash {job.content_hash[:8]}...)",
        ))
        return True

    # --------------------------------------------------------------------------
    # Stage 3: Pre-Processing
    # --------------------------------------------------------------------------

    async def stage_3_preprocess(self, job: JobPosting) -> JobPosting:
        """
        Stage 3: Pre-process job posting using TextTruncator.

        - Strips HTML, legal, EEO boilerplate, and recruiter disclaimers.
        - Validates content length (>50 characters).
        - Enforces context ceiling (max_tokens).
        - Wraps in XML delimiters (`<job_posting>`) to neutralize prompt injection.
        - Sets job.status = JobStatus.PREPROCESSED.
        - Flushes immediately to DuckDB.

        Args:
            job: Target JobPosting entity.

        Returns:
            Updated JobPosting entity.
        """
        try:
            trunc_result = self.truncator.process(
                job.raw_description,
                wrap_xml=self.config.auto_wrap_xml,
            )
            job.cleaned_description = trunc_result.final_text
            job.description = trunc_result.final_text
            job.is_truncated = trunc_result.was_truncated
            job.token_count = trunc_result.final_tokens
            job.status = JobStatus.PREPROCESSED

            await self.repository.update_job(job)
            await self.emit(PipelineEvent(
                event_type=PipelineEventType.JOB_PREPROCESSED,
                job=job,
                stage_number=3,
                message=f"Job pre-processed (tokens: {job.token_count}, truncated: {job.is_truncated})",
                data={"tokens": job.token_count, "truncated": job.is_truncated},
            ))

        except DescriptionTooShortError as exc:
            job.status = JobStatus.FAILED
            job.error_message = f"Pre-processing failed: {exc}"
            await self.repository.update_status(
                job_id=job.id,
                status=JobStatus.FAILED,
                error_message=job.error_message,
            )
            await self.emit(PipelineEvent(
                event_type=PipelineEventType.JOB_ERROR,
                job=job,
                stage_number=3,
                message=job.error_message,
            ))

        except Exception as exc:  # noqa: BLE001
            job.status = JobStatus.ERROR
            job.error_message = f"Pre-processing error: {exc}"
            await self.repository.update_status(
                job_id=job.id,
                status=JobStatus.ERROR,
                error_message=job.error_message,
            )
            await self.emit(PipelineEvent(
                event_type=PipelineEventType.JOB_ERROR,
                job=job,
                stage_number=3,
                message=job.error_message,
            ))

        return job

    # --------------------------------------------------------------------------
    # Stage 4: Triage
    # --------------------------------------------------------------------------

    async def stage_4_triage(self, job: JobPosting) -> JobPosting:
        """
        Stage 4: Triage candidate fit using local LLM via JobFitEvaluator.

        - Guarded by LocalLoopGuard with timeout protection.
        - Catches TimeoutError / LLMTimeoutError:
          - Marks job.status = JobStatus.SKIPPED_TIMEOUT.
          - Flushes immediately to DuckDB.
          - Emits JOB_SKIPPED_TIMEOUT event and gracefully skips without crashing.
        - On success:
          - Sets job.evaluation, job.fit_score, job.recommendation.
          - Sets job.status = JobStatus.TRIAGED.
          - Flushes immediately to DuckDB.
          - Emits JOB_TRIAGED event.

        Args:
            job: Target JobPosting entity.

        Returns:
            Updated JobPosting entity.
        """
        job_desc = job.description or job.cleaned_description or job.raw_description

        await self.emit(PipelineEvent(
            event_type=PipelineEventType.JOB_TRIAGE_STARTED,
            job=job,
            stage_number=4,
            message=f"Evaluating fit for '{job.title}' against candidate profile",
        ))

        try:
            if self.guard is not None and hasattr(self.guard, "run_guarded"):
                evaluation = await self.guard.run_guarded(
                    self.evaluator.evaluate_fit,
                    job_desc,
                    self.candidate_profile,
                    job=job,
                    step_name="triage",
                    repository=self.repository,
                )
                if evaluation is None:
                    if job.status == JobStatus.SKIPPED_TIMEOUT:
                        await self.emit(PipelineEvent(
                            event_type=PipelineEventType.JOB_SKIPPED_TIMEOUT,
                            job=job,
                            stage_number=4,
                            message=job.error_message or "Triage timed out",
                        ))
                    return job
            else:
                async with asyncio.timeout(self.config.timeout_seconds):
                    evaluation = await self.evaluator.evaluate_fit(
                        job_desc,
                        self.candidate_profile,
                    )

            job.evaluation = evaluation
            job.fit_score = evaluation.fit_score
            job.recommendation = evaluation.recommendation
            job.status = JobStatus.TRIAGED

            await self.repository.update_job(job)
            await self.emit(PipelineEvent(
                event_type=PipelineEventType.JOB_TRIAGED,
                job=job,
                stage_number=4,
                message=f"Job triaged: fit_score={job.fit_score}, recommendation={job.recommendation}",
                data={
                    "fit_score": job.fit_score,
                    "recommendation": job.recommendation.value if job.recommendation else None,
                    "matched_skills": evaluation.matched_skills,
                    "missing_skills": evaluation.missing_skills,
                },
            ))

        except (TimeoutError, LLMTimeoutError) as exc:
            job.status = JobStatus.SKIPPED_TIMEOUT
            job.error_message = f"Triage timed out after {self.config.timeout_seconds}s: {exc}"
            await self.repository.update_status(
                job_id=job.id,
                status=JobStatus.SKIPPED_TIMEOUT,
                error_message=job.error_message,
            )
            await self.emit(PipelineEvent(
                event_type=PipelineEventType.JOB_SKIPPED_TIMEOUT,
                job=job,
                stage_number=4,
                message=job.error_message,
            ))

        except LLMConnectionError as exc:
            job.status = JobStatus.ERROR
            job.error_message = f"LLM connection error: {exc}"
            await self.repository.update_status(
                job_id=job.id,
                status=JobStatus.ERROR,
                error_message=job.error_message,
            )
            await self.emit(PipelineEvent(
                event_type=PipelineEventType.JOB_ERROR,
                job=job,
                stage_number=4,
                message=job.error_message,
            ))
            if self.config.stop_on_llm_connection_error:
                raise

        except Exception as exc:  # noqa: BLE001
            job.status = JobStatus.ERROR
            job.error_message = f"Triage error: {exc}"
            await self.repository.update_status(
                job_id=job.id,
                status=JobStatus.ERROR,
                error_message=job.error_message,
            )
            await self.emit(PipelineEvent(
                event_type=PipelineEventType.JOB_ERROR,
                job=job,
                stage_number=4,
                message=job.error_message,
            ))

        return job

    # --------------------------------------------------------------------------
    # Stage 5: Decision Tree
    # --------------------------------------------------------------------------

    async def stage_5_decision_tree(self, job: JobPosting) -> JobPosting:
        """
        Stage 5: Decision Tree routing to SHORTLISTED vs DISCARDED.

        - If fit_score >= score_threshold:
          - Sets job.status = JobStatus.SHORTLISTED
          - Sets job.recommendation = Recommendation.SHORTLIST
          - Emits JOB_SHORTLISTED event
        - Else:
          - Sets job.status = JobStatus.DISCARDED
          - Sets job.recommendation = Recommendation.DISCARD
          - Emits JOB_DISCARDED event
        - Immediately flushes decision to DuckDB.

        Args:
            job: Target JobPosting entity.

        Returns:
            Updated JobPosting entity.
        """
        if job.fit_score is not None and job.fit_score >= self.score_threshold:
            job.status = JobStatus.SHORTLISTED
            job.recommendation = Recommendation.SHORTLIST
            event_type = PipelineEventType.JOB_SHORTLISTED
            msg = f"Job SHORTLISTED (fit_score={job.fit_score} >= {self.score_threshold})"
        else:
            job.status = JobStatus.DISCARDED
            job.recommendation = Recommendation.DISCARD
            event_type = PipelineEventType.JOB_DISCARDED
            msg = f"Job DISCARDED (fit_score={job.fit_score} < {self.score_threshold})"

        await self.repository.update_status(
            job_id=job.id,
            status=job.status,
            fit_score=job.fit_score,
            recommendation=job.recommendation,
        )

        await self.emit(PipelineEvent(
            event_type=event_type,
            job=job,
            stage_number=5,
            message=msg,
            data={
                "fit_score": job.fit_score,
                "threshold": self.score_threshold,
                "decision": job.recommendation.value if job.recommendation else None,
            },
        ))
        return job

    # --------------------------------------------------------------------------
    # Single Job Processing (Stages 2 -> 5)
    # --------------------------------------------------------------------------

    async def process_job(self, job: JobPosting) -> JobPosting:
        """
        Execute Stages 2 through 5 for a single job posting.

        Flow:
        - Stage 2: Deduplication. If duplicate, halts and returns.
        - Stage 3: Pre-Processing. If failed, halts and returns.
        - Stage 4: Triage. If timeout or error, halts and returns.
        - Stage 5: Decision Tree. Finalizes SHORTLISTED or DISCARDED.

        Args:
            job: JobPosting entity.

        Returns:
            JobPosting after processing.
        """
        # Stage 2: Deduplicate
        is_unique = await self.stage_2_deduplicate(job)
        if not is_unique:
            return job

        # Stage 3: Pre-process
        job = await self.stage_3_preprocess(job)
        if job.status in (JobStatus.FAILED, JobStatus.ERROR):
            return job

        # Stage 4: Triage
        job = await self.stage_4_triage(job)
        if job.status in (JobStatus.SKIPPED_TIMEOUT, JobStatus.ERROR, JobStatus.FAILED):
            return job

        # Stage 5: Decision Tree
        job = await self.stage_5_decision_tree(job)
        return job

    # --------------------------------------------------------------------------
    # Full Execution Loop
    # --------------------------------------------------------------------------

    async def run(
        self,
        limit: int | None = None,
        batch_size: int | None = None,
    ) -> PipelineResult:
        """
        Execute the full closed-loop pipeline orchestrator.

        Pulls jobs from `ingestion_client` in bounded batches, executes
        Stages 2-5 per job, enforces `LocalLoopGuard` iteration limits, and emits
        telemetry events.

        Guarantees O(1) RAM consumption by flushing and dereferencing jobs.

        Args:
            limit: Maximum total jobs to process (defaults to guard.max_iterations or config).
            batch_size: Ingestion batch chunk size (defaults to config.batch_size).

        Returns:
            PipelineResult summary metrics.
        """
        start_time = time.monotonic()
        result = PipelineResult()

        eff_batch = batch_size or self.config.batch_size
        eff_limit = limit or (
            self.guard.max_iterations
            if self.guard and hasattr(self.guard, "max_iterations")
            else self.config.max_iterations
        )

        await self.emit(PipelineEvent(
            event_type=PipelineEventType.PIPELINE_STARTED,
            message=f"Pipeline started (limit={eff_limit}, batch_size={eff_batch})",
            data={"limit": eff_limit, "batch_size": eff_batch},
        ))

        try:
            while result.total_processed < eff_limit:
                # Check guard iteration limits
                if (
                    self.guard is not None
                    and hasattr(self.guard, "can_continue")
                    and not self.guard.can_continue()
                ):
                    await self.emit(PipelineEvent(
                        event_type=PipelineEventType.PIPELINE_STOPPED,
                        message="Pipeline stopped: LocalLoopGuard iteration limit reached",
                        data={"iterations": getattr(self.guard, "current_iteration", 0)},
                    ))
                    break

                remaining = eff_limit - result.total_processed
                fetch_count = min(eff_batch, remaining)

                try:
                    batch = await self.stage_1_ingest(limit=fetch_count)
                except CircuitOpenError as circ_err:
                    logger.warning("Pipeline paused: MCP Circuit Breaker is OPEN: %s", circ_err)
                    await self.emit(PipelineEvent(
                        event_type=PipelineEventType.PIPELINE_STOPPED,
                        message=f"Pipeline stopped: MCP Circuit Breaker is OPEN: {circ_err}",
                    ))
                    break
                except Exception as ing_err:  # noqa: BLE001
                    logger.error("Ingestion stage error: %s", ing_err)
                    await self.emit(PipelineEvent(
                        event_type=PipelineEventType.PIPELINE_STOPPED,
                        message=f"Pipeline stopped on ingestion error: {ing_err}",
                    ))
                    break

                if not batch:
                    # Ingestion source exhausted
                    break

                for job in batch:
                    if (
                        self.guard is not None
                        and hasattr(self.guard, "can_continue")
                        and not self.guard.can_continue()
                    ):
                        break

                    processed_job = await self.process_job(job)
                    result.total_processed += 1
                    result.total_ingested += 1

                    status = processed_job.status
                    if status == JobStatus.DUPLICATE:
                        result.duplicates += 1
                    elif status == JobStatus.SHORTLISTED:
                        result.shortlisted += 1
                        result.triaged += 1
                        result.preprocessed += 1
                    elif status == JobStatus.DISCARDED:
                        result.discarded += 1
                        result.triaged += 1
                        result.preprocessed += 1
                    elif status == JobStatus.SKIPPED_TIMEOUT:
                        result.skipped_timeout += 1
                        result.preprocessed += 1
                    elif status in (JobStatus.ERROR, JobStatus.FAILED):
                        result.errors += 1
                    elif status == JobStatus.PREPROCESSED:
                        result.preprocessed += 1
                    elif status == JobStatus.TRIAGED:
                        result.triaged += 1

                    # Ensure O(1) RAM: explicitly drop job references
                    del processed_job
                    del job

        finally:
            result.duration_seconds = time.monotonic() - start_time
            await self.emit(PipelineEvent(
                event_type=PipelineEventType.PIPELINE_COMPLETED,
                message=(
                    f"Pipeline completed in {result.duration_seconds:.2f}s: "
                    f"{result.shortlisted} shortlisted, {result.discarded} discarded, "
                    f"{result.duplicates} duplicates, {result.skipped_timeout} timeouts, "
                    f"{result.errors} errors"
                ),
                data=result.to_dict(),
            ))

        return result

    async def process_stream(
        self,
        job_stream: AsyncIterator[JobPosting],
        limit: int | None = None,
    ) -> PipelineResult:
        """
        Process an asynchronous stream / generator of job postings with O(1) memory.

        Args:
            job_stream: AsyncIterator yielding JobPosting domain entities.
            limit: Maximum number of jobs to process from the stream.

        Returns:
            PipelineResult summary metrics.
        """
        start_time = time.monotonic()
        result = PipelineResult()
        eff_limit = limit or (
            self.guard.max_iterations
            if self.guard and hasattr(self.guard, "max_iterations")
            else self.config.max_iterations
        )

        await self.emit(PipelineEvent(
            event_type=PipelineEventType.PIPELINE_STARTED,
            message=f"Pipeline stream started (limit={eff_limit})",
            data={"limit": eff_limit},
        ))

        try:
            async for job in job_stream:
                if result.total_processed >= eff_limit:
                    break

                if (
                    self.guard is not None
                    and hasattr(self.guard, "can_continue")
                    and not self.guard.can_continue()
                ):
                    await self.emit(PipelineEvent(
                        event_type=PipelineEventType.PIPELINE_STOPPED,
                        message="Stream stopped: LocalLoopGuard iteration limit reached",
                    ))
                    break

                processed_job = await self.process_job(job)
                result.total_processed += 1
                result.total_ingested += 1

                status = processed_job.status
                if status == JobStatus.DUPLICATE:
                    result.duplicates += 1
                elif status == JobStatus.SHORTLISTED:
                    result.shortlisted += 1
                    result.triaged += 1
                    result.preprocessed += 1
                elif status == JobStatus.DISCARDED:
                    result.discarded += 1
                    result.triaged += 1
                    result.preprocessed += 1
                elif status == JobStatus.SKIPPED_TIMEOUT:
                    result.skipped_timeout += 1
                    result.preprocessed += 1
                elif status in (JobStatus.ERROR, JobStatus.FAILED):
                    result.errors += 1
                elif status == JobStatus.PREPROCESSED:
                    result.preprocessed += 1
                elif status == JobStatus.TRIAGED:
                    result.triaged += 1

                del processed_job
                del job

        finally:
            result.duration_seconds = time.monotonic() - start_time
            await self.emit(PipelineEvent(
                event_type=PipelineEventType.PIPELINE_COMPLETED,
                message=f"Pipeline stream completed in {result.duration_seconds:.2f}s",
                data=result.to_dict(),
            ))

        return result
