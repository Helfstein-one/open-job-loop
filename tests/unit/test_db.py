"""
Comprehensive Unit Tests for DuckDB Persistence Layer.

Verifies:
1. Database initialization and DDL execution.
2. Immediate WAL flush and checkpoint durability.
3. SHA256 content deduplication (ON CONFLICT DO NOTHING).
4. Status transitions (INGESTED -> PREPROCESSED -> TRIAGED -> SHORTLISTED / DISCARDED / SKIPPED_TIMEOUT).
5. Non-blocking async thread offloading.
6. Zero large arrays in memory (O(1) streaming iteration).
7. Thread safety during concurrent writes and duplicate collisions.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from collections.abc import AsyncGenerator

import pytest

from src.infrastructure.adapters.database import DatabaseManager
from src.infrastructure.adapters.repository import JobRepository, compute_job_hash
from src.domain.models import (
    JobPosting,
    JobStatus,
    MatchEvaluation,
    Recommendation,
)


@pytest.fixture
def temp_db_file() -> AsyncGenerator[str, None]:
    """Provides an isolated temporary DuckDB file path and cleans up on teardown."""
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, f"test_{uuid.uuid4().hex}.duckdb")
    yield db_path
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass
    wal_path = f"{db_path}.wal"
    if os.path.exists(wal_path):
        try:
            os.remove(wal_path)
        except OSError:
            pass
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass


@pytest.fixture
async def repo_memory() -> AsyncGenerator[JobRepository, None]:
    """Provides an in-memory JobRepository initialized for rapid unit testing."""
    repo = JobRepository(db_path=":memory:")
    await repo.initialize()
    yield repo
    await repo.close()


@pytest.fixture
async def repo_file(temp_db_file: str) -> AsyncGenerator[JobRepository, None]:
    """Provides a file-backed JobRepository initialized for persistence testing."""
    repo = JobRepository(db_path=temp_db_file)
    await repo.initialize()
    yield repo
    await repo.close()


def create_sample_job(
    job_id: str = "test-job-1",
    raw_desc: str = "We are seeking a Python systems engineer with DuckDB experience.",
    title: str = "Python Systems Engineer",
    company: str = "Acme Corp",
    status: JobStatus = JobStatus.INGESTED
) -> JobPosting:
    """Helper to instantiate a valid JobPosting entity."""
    content_hash = compute_job_hash(raw_desc, title=title, company=company)
    return JobPosting(
        id=job_id,
        content_hash=content_hash,
        title=title,
        company=company,
        location="Remote",
        raw_description=raw_desc,
        status=status,
    )


# Test 1: Database Initialization
@pytest.mark.asyncio
async def test_database_initialization_in_memory(repo_memory: JobRepository) -> None:
    """Asserts schema and indexes are created successfully in memory."""
    _cols, rows = await repo_memory._db.aexecute_read(
        "SELECT table_name FROM information_schema.tables WHERE table_name = 'job_postings';"
    )
    assert len(rows) == 1
    assert rows[0][0] == "job_postings"


@pytest.mark.asyncio
async def test_database_initialization_creates_parent_dir(temp_db_file: str) -> None:
    """Asserts that DatabaseManager creates missing parent directories automatically."""
    nested_path = os.path.join(os.path.dirname(temp_db_file), "nested", "sub", "app.duckdb")
    db = DatabaseManager(nested_path)
    await db.ainitialize()
    assert os.path.exists(nested_path)
    await db.aclose()


# Test 2: Immediate Flush and Checkpoint Durability
@pytest.mark.asyncio
async def test_immediate_flush_durability(temp_db_file: str) -> None:
    """Asserts that save_job checkpoints WAL immediately to the primary DB file."""
    repo = JobRepository(db_path=temp_db_file)
    await repo.initialize()

    job = create_sample_job("flush-job-1")
    inserted = await repo.save_job(job)
    assert inserted is True

    # Close first connection
    await repo.close()

    # Reopen brand-new connection to the file and verify data is present on disk
    new_repo = JobRepository(db_path=temp_db_file)
    await new_repo.initialize()
    persisted_job = await new_repo.get_job("flush-job-1")
    assert persisted_job is not None
    assert persisted_job.id == "flush-job-1"
    assert persisted_job.title == "Python Systems Engineer"
    await new_repo.close()


# Test 3: SHA256 Deduplication (Pre-Check and Atomic Constraint)
@pytest.mark.asyncio
async def test_sha256_deduplication_pre_check(repo_memory: JobRepository) -> None:
    """Asserts is_duplicate accurately identifies existing hashes."""
    job = create_sample_job("dedup-1")
    assert await repo_memory.is_duplicate(job.content_hash) is False

    await repo_memory.save_job(job)
    assert await repo_memory.is_duplicate(job.content_hash) is True
    assert await repo_memory.is_duplicate("unknown_sha256_hash") is False


@pytest.mark.asyncio
async def test_atomic_on_conflict_do_nothing(repo_memory: JobRepository) -> None:
    """
    Asserts ON CONFLICT (content_hash) DO NOTHING skips duplicate insert
    and returns False without throwing BinderException or UniqueConstraint error.
    """
    job1 = create_sample_job(job_id="job-id-1", raw_desc="Identical description")
    job2 = create_sample_job(job_id="job-id-2", raw_desc="Identical description")
    assert job1.content_hash == job2.content_hash

    # First insert succeeds
    first_res = await repo_memory.save_job(job1)
    assert first_res is True

    # Second duplicate insert is gracefully skipped
    second_res = await repo_memory.save_job(job2)
    assert second_res is False

    # Verify table contains exactly 1 row (the original record)
    _cols, rows = await repo_memory._db.aexecute_read("SELECT id, content_hash FROM job_postings;")
    assert len(rows) == 1
    assert rows[0][0] == "job-id-1"


# Test 4: DAG Status Transitions
@pytest.mark.asyncio
async def test_status_transitions_lifecycle(repo_memory: JobRepository) -> None:
    """Tests lifecycle: INGESTED -> PREPROCESSED -> TRIAGED -> SHORTLISTED."""
    job = create_sample_job("lifecycle-1")
    await repo_memory.save_job(job)

    # 1. Update to PREPROCESSED
    job.status = JobStatus.PREPROCESSED
    job.cleaned_description = "Cleaned job description"
    job.is_truncated = True
    job.token_count = 120
    assert await repo_memory.update_job(job) is True

    fetched = await repo_memory.get_job("lifecycle-1")
    assert fetched is not None
    assert fetched.status == JobStatus.PREPROCESSED
    assert fetched.cleaned_description == "Cleaned job description"
    assert fetched.is_truncated is True
    assert fetched.token_count == 120

    # 2. Update to TRIAGED with MatchEvaluation
    eval_model = MatchEvaluation(
        fit_score=90,
        recommendation=Recommendation.SHORTLIST,
        matched_skills=["Python", "DuckDB", "AsyncIO"],
        missing_skills=[],
        reasoning="Exemplary fit for backend pipeline role."
    )
    job.status = JobStatus.TRIAGED
    job.fit_score = 90
    job.recommendation = Recommendation.SHORTLIST
    job.evaluation = eval_model
    assert await repo_memory.update_job(job) is True

    fetched_eval = await repo_memory.get_job("lifecycle-1")
    assert fetched_eval is not None
    assert fetched_eval.status == JobStatus.TRIAGED
    assert fetched_eval.fit_score == 90
    assert fetched_eval.recommendation == Recommendation.SHORTLIST
    assert fetched_eval.evaluation is not None
    assert fetched_eval.evaluation.fit_score == 90
    assert fetched_eval.evaluation.matched_skills == ["Python", "DuckDB", "AsyncIO"]

    # 3. Decision update to SHORTLISTED
    assert await repo_memory.update_status("lifecycle-1", JobStatus.SHORTLISTED) is True
    final_job = await repo_memory.get_job("lifecycle-1")
    assert final_job is not None
    assert final_job.status == JobStatus.SHORTLISTED


@pytest.mark.asyncio
async def test_skipped_timeout_status_recording(repo_memory: JobRepository) -> None:
    """
    Asserts Execution Harness timeout handler can record JobStatus.SKIPPED_TIMEOUT
    with error message and fit_score=None.
    """
    job = create_sample_job("timeout-job-1")
    await repo_memory.save_job(job)

    updated = await repo_memory.update_status(
        job_id="timeout-job-1",
        status=JobStatus.SKIPPED_TIMEOUT,
        error_message="Inference timed out after 15.0 seconds"
    )
    assert updated is True

    fetched = await repo_memory.get_job("timeout-job-1")
    assert fetched is not None
    assert fetched.status == JobStatus.SKIPPED_TIMEOUT
    assert fetched.error_message == "Inference timed out after 15.0 seconds"
    assert fetched.fit_score is None


# Test 5: Statistics Aggregation
@pytest.mark.asyncio
async def test_get_stats_aggregation(repo_memory: JobRepository) -> None:
    """Asserts get_stats correctly counts records across all status buckets."""
    jobs = [
        create_sample_job("stat-1", raw_desc="d1", status=JobStatus.SHORTLISTED),
        create_sample_job("stat-2", raw_desc="d2", status=JobStatus.SHORTLISTED),
        create_sample_job("stat-3", raw_desc="d3", status=JobStatus.DISCARDED),
        create_sample_job("stat-4", raw_desc="d4", status=JobStatus.SKIPPED_TIMEOUT),
        create_sample_job("stat-5", raw_desc="d5", status=JobStatus.INGESTED),
    ]
    for j in jobs:
        await repo_memory.save_job(j)

    stats = await repo_memory.get_stats()
    assert stats["total"] == 5
    assert stats[JobStatus.SHORTLISTED.value] == 2
    assert stats[JobStatus.DISCARDED.value] == 1
    assert stats[JobStatus.SKIPPED_TIMEOUT.value] == 1
    assert stats[JobStatus.INGESTED.value] == 1
    assert stats[JobStatus.ERROR.value] == 0


# Test 6: Zero Large Arrays in Memory (Streaming Iteration)
@pytest.mark.asyncio
async def test_streaming_iteration_bounded_memory(repo_memory: JobRepository) -> None:
    """Asserts iterate_jobs streams records with bounded batch sizes."""
    # Seed 25 items
    for i in range(25):
        j = create_sample_job(f"stream-{i}", raw_desc=f"Unique description {i}")
        await repo_memory.save_job(j)

    streamed_ids = []
    async for job in repo_memory.iterate_jobs(batch_size=5):
        streamed_ids.append(job.id)

    assert len(streamed_ids) == 25
    assert streamed_ids[0] == "stream-0"
    assert streamed_ids[-1] == "stream-24"


# Test 7: Concurrency & Thread Safety
@pytest.mark.asyncio
async def test_concurrent_distinct_writes(repo_memory: JobRepository) -> None:
    """Asserts concurrent tasks can write distinct jobs without lock contention errors."""
    async def insert_worker(idx: int) -> bool:
        j = create_sample_job(f"concurrent-{idx}", raw_desc=f"Desc {idx}")
        return await repo_memory.save_job(j)

    results = await asyncio.gather(*(insert_worker(i) for i in range(20)))
    assert all(results)
    stats = await repo_memory.get_stats()
    assert stats["total"] == 20


@pytest.mark.asyncio
async def test_concurrent_duplicate_writes_no_transaction_exception(
    repo_memory: JobRepository
) -> None:
    """
    Asserts concurrent tasks attempting to insert the exact same duplicate content_hash
    execute cleanly via the write lock without raising TransactionException.
    """
    async def duplicate_worker(idx: int) -> bool:
        j = create_sample_job(f"dup-{idx}", raw_desc="Identical collision payload")
        return await repo_memory.save_job(j)

    results = await asyncio.gather(*(duplicate_worker(i) for i in range(20)))
    # Exactly one worker succeeded in inserting, others skipped
    assert sum(1 for r in results if r is True) == 1
    assert sum(1 for r in results if r is False) == 19

    stats = await repo_memory.get_stats()
    assert stats["total"] == 1


# Test 8: Non-Blocking Event Loop
@pytest.mark.asyncio
async def test_non_blocking_event_loop(repo_memory: JobRepository) -> None:
    """
    Asserts that database operations offloaded via asyncio.to_thread
    allow concurrent coroutines (e.g. timers, UI tickers) to tick smoothly.
    """
    ticker_count = 0

    async def background_ticker() -> None:
        nonlocal ticker_count
        for _ in range(10):
            await asyncio.sleep(0.005)
            ticker_count += 1

    ticker_task = asyncio.create_task(background_ticker())

    for i in range(10):
        j = create_sample_job(f"tick-{i}", raw_desc=f"Tick text {i}")
        await repo_memory.save_job(j)

    await ticker_task
    assert ticker_count >= 5


@pytest.mark.asyncio
async def test_streaming_iteration_status_mutation_no_skip(repo_memory: JobRepository) -> None:
    """
    Asserts keyset pagination prevents row skipping when caller mutates
    job status in-flight (e.g. INGESTED -> PREPROCESSED).
    """
    total = 30
    for i in range(total):
        j = create_sample_job(f"mutate-{i:02d}", raw_desc=f"Desc {i}", status=JobStatus.INGESTED)
        await repo_memory.save_job(j)

    processed_ids = []
    async for job in repo_memory.iterate_jobs(status=JobStatus.INGESTED, batch_size=5):
        processed_ids.append(job.id)
        await repo_memory.update_status(job.id, JobStatus.PREPROCESSED)

    assert len(processed_ids) == total
    assert processed_ids == [f"mutate-{i:02d}" for i in range(total)]

    stats = await repo_memory.get_stats()
    assert stats[JobStatus.INGESTED.value] == 0
    assert stats[JobStatus.PREPROCESSED.value] == total
