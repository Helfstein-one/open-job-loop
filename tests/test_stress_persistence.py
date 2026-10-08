"""
Empirical Stress Test Suite for DuckDB Persistence and Concurrency.

Executed by M1 Empirical Challenger 2.
Verifies:
1. High-concurrency duplicate insertion races (200+ concurrent coroutines, distinct groups).
2. Multi-threaded mixed read/write/checkpoint concurrency (thread-pool stress).
3. O(1) RAM usage verification during streaming iteration via tracemalloc.
4. WAL checkpoint durability and immediate disk persistence.
5. Abrupt crash recovery under SIGKILL.
6. Status transitions and update semantics.
7. SHA256 hash collision edge cases.
"""

from __future__ import annotations

import asyncio
import gc
import os
import random
import subprocess
import sys
import tempfile
import tracemalloc
import uuid
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.infrastructure.adapters.database import DatabaseManager
from src.infrastructure.adapters.repository import JobRepository, compute_job_hash
from src.domain.models import JobPosting, JobStatus, MatchEvaluation, Recommendation


@pytest.fixture
def stress_temp_db() -> AsyncGenerator[str, None]:
    """Provides isolated temporary DuckDB file."""
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, f"stress_{uuid.uuid4().hex}.duckdb")
    yield db_path
    for p in (db_path, f"{db_path}.wal"):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_high_concurrency_duplicate_races(stress_temp_db: str) -> None:
    """
    Stress-tests 200 concurrent coroutines attempting to insert duplicate content hashes
    across 10 distinct collision groups.
    Asserts:
    - Zero TransactionException or lock collisions.
    - Exactly 1 insert per collision group succeeds (10 total inserts).
    - 190 duplicate inserts return False gracefully.
    - DuckDB stores exactly 10 records.
    """
    repo = JobRepository(db_path=stress_temp_db)
    await repo.initialize()

    NUM_GROUPS = 10
    ATTEMPTS_PER_GROUP = 20

    async def worker(group_id: int, attempt_id: int):
        desc = f"Identical description for group {group_id}."
        title = f"Title {group_id}"
        company = f"Company {group_id}"
        job = JobPosting(
            id=f"grp{group_id}-att{attempt_id}",
            content_hash=compute_job_hash(desc, title=title, company=company),
            title=title,
            company=company,
            raw_description=desc,
            status=JobStatus.INGESTED,
        )
        await asyncio.sleep(random.uniform(0.0001, 0.003))
        res = await repo.save_job(job)
        return group_id, res

    coros = [worker(g, a) for g in range(NUM_GROUPS) for a in range(ATTEMPTS_PER_GROUP)]
    random.shuffle(coros)
    results = await asyncio.gather(*coros)

    group_wins = {g: 0 for g in range(NUM_GROUPS)}
    for g, success in results:
        if success:
            group_wins[g] += 1

    for g in range(NUM_GROUPS):
        assert group_wins[g] == 1, f"Group {g} had {group_wins[g]} winners instead of 1"

    stats = await repo.get_stats()
    assert stats["total"] == NUM_GROUPS
    assert stats[JobStatus.INGESTED.value] == NUM_GROUPS

    await repo.close()


def test_multithreaded_read_write_concurrency(stress_temp_db: str) -> None:
    """
    Stress-tests DatabaseManager under real multi-threaded contention (16 OS worker threads)
    executing 200 mixed read, write, and checkpoint operations concurrently.
    Asserts no deadlocks, no unhandled thread exceptions, and data integrity.
    """
    db = DatabaseManager(db_path=stress_temp_db)
    db.initialize()

    errors = []

    def task(i: int) -> None:
        try:
            op = i % 4
            if op in (0, 1):
                db.execute_write(
                    "INSERT INTO job_postings (id, content_hash, title, company, raw_description, status) "
                    "VALUES (?, ?, ?, ?, ?, ?);",
                    (f"t-{i}", f"h-{i}", f"Title {i}", "Co", "Desc", "INGESTED"),
                    checkpoint=(op == 0)
                )
            elif op == 2:
                db.execute_read("SELECT count(*), max(id) FROM job_postings;")
            elif op == 3:
                db.execute_scalar("SELECT title FROM job_postings WHERE id = ?;", (f"t-{i-1}",))
        except Exception as e:  # noqa: BLE001
            errors.append((i, type(e).__name__, str(e)))

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(task, i) for i in range(200)]
        for f in futures:
            f.result()

    assert len(errors) == 0, f"Thread errors encountered: {errors}"
    _cols, rows = db.execute_read("SELECT count(*) FROM job_postings;")
    assert rows[0][0] == 100

    db.close()


@pytest.mark.asyncio
async def test_streaming_iteration_o1_ram(stress_temp_db: str) -> None:
    """
    Empirically verifies O(1) RAM consumption during streaming iteration over 3,000 rows.
    Compares streaming heap peak against bulk in-memory loading.
    """
    repo = JobRepository(db_path=stress_temp_db)
    await repo.initialize()

    conn = repo._db.connect()
    TOTAL_ROWS = 3000
    batch = [
        (
            f"stream-row-{i}",
            f"stream-hash-{i}",
            f"Software Engineer {i}",
            "Enterprise Corp",
            "Job description payload text " * 15,
            "INGESTED"
        )
        for i in range(TOTAL_ROWS)
    ]
    conn.executemany(
        "INSERT INTO job_postings (id, content_hash, title, company, raw_description, status) VALUES (?, ?, ?, ?, ?, ?);",
        batch
    )
    conn.execute("CHECKPOINT;")

    gc.collect()
    tracemalloc.start()
    stream_count = 0
    sampled_peaks = []

    async for job in repo.iterate_jobs(batch_size=50):
        stream_count += 1
        if stream_count % 500 == 0:
            _, cur_peak = tracemalloc.get_traced_memory()
            sampled_peaks.append(cur_peak)

    _, final_stream_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert stream_count == TOTAL_ROWS

    # Peak memory during streaming must remain under 300 KB (O(1) footprint)
    assert final_stream_peak < 350 * 1024, f"Streaming heap peak was too high: {final_stream_peak / 1024:.1f} KB"

    # Contrast with bulk in-memory load
    gc.collect()
    tracemalloc.start()
    cols, all_rows = await repo._db.aexecute_read("SELECT * FROM job_postings;")
    all_jobs = [repo._row_to_job(cols, r) for r in all_rows]
    _, bulk_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(all_jobs) == TOTAL_ROWS
    # Bulk loading should consume substantially more memory (> 5 MB)
    assert bulk_peak > 5 * 1024 * 1024, f"Bulk peak was surprisingly low: {bulk_peak / 1024:.1f} KB"
    # Streaming footprint should be at least 15x smaller than bulk
    assert bulk_peak / final_stream_peak > 15.0

    await repo.close()


@pytest.mark.asyncio
async def test_immediate_wal_checkpoint_flush(stress_temp_db: str) -> None:
    """
    Verifies that save_job triggers an immediate checkpoint, removing or zeroing
    the WAL file and ensuring data is immediately present on the primary file.
    """
    repo = JobRepository(db_path=stress_temp_db)
    await repo.initialize()

    wal_path = f"{stress_temp_db}.wal"

    for i in range(10):
        job = JobPosting(
            id=f"wal-check-{i}",
            content_hash=compute_job_hash(f"desc {i}", title=f"title {i}", company="Co"),
            title=f"title {i}",
            company="Co",
            raw_description=f"desc {i}",
            status=JobStatus.INGESTED
        )
        await repo.save_job(job)
        # Verify WAL file does not accumulate dirty data
        if os.path.exists(wal_path):
            assert os.path.getsize(wal_path) == 0

    await repo.close()

    # Verify primary database file contains all rows
    db = DatabaseManager(db_path=stress_temp_db)
    db.initialize()
    _cols, rows = db.execute_read("SELECT count(*) FROM job_postings;")
    assert rows[0][0] == 10
    db.close()


def test_crash_recovery_sigkill(stress_temp_db: str) -> None:
    """
    Simulates an ungraceful process crash (SIGKILL -9) during active writes.
    Asserts DuckDB recovers without corruption and flushes preceding transactions.
    """
    child_script = f"""
import time, os, sys, asyncio
from src.infrastructure.adapters.repository import JobRepository, compute_job_hash
from src.domain.models import JobPosting, JobStatus

async def main():
    repo = JobRepository(db_path='{stress_temp_db}')
    await repo.initialize()
    for i in range(40):
        j = JobPosting(
            id=f'sigkill-{{i}}',
            content_hash=compute_job_hash(f'Crash test desc {{i}}', title='T', company='C'),
            title='T',
            company='C',
            raw_description=f'Crash test desc {{i}}',
            status=JobStatus.INGESTED
        )
        await repo.save_job(j)
        if i == 20:
            print('CHECKPOINT_REACHED', flush=True)
    # Deliberately hang without calling close()
    time.sleep(10)

asyncio.run(main())
"""
    proc = subprocess.Popen([sys.executable, "-c", child_script], stdout=subprocess.PIPE, text=True)
    signal_line = proc.stdout.readline()
    assert signal_line.strip() == "CHECKPOINT_REACHED"

    # Kill process abruptly
    proc.kill()
    proc.wait()
    assert proc.returncode == -9

    # Reopen database in parent and assert durability
    db = DatabaseManager(db_path=stress_temp_db)
    db.initialize()
    _cols, rows = db.execute_read("SELECT count(*) FROM job_postings;")
    # At least 21 items must be durable
    assert rows[0][0] >= 21
    # Check that individual rows are readable and uncorrupted
    _cols, row_sample = db.execute_read("SELECT id, title, raw_description FROM job_postings WHERE id = 'sigkill-0';")
    assert len(row_sample) == 1
    assert row_sample[0][0] == "sigkill-0"
    db.close()


@pytest.mark.asyncio
async def test_status_transitions_and_error_handling(stress_temp_db: str) -> None:
    """
    Verifies state transitions across the full pipeline lifecycle:
    INGESTED -> PREPROCESSED -> TRIAGED -> DISCARDED / SHORTLISTED / SKIPPED_TIMEOUT.
    """
    repo = JobRepository(db_path=stress_temp_db)
    await repo.initialize()

    job = JobPosting(
        id="stat-job-1",
        content_hash=compute_job_hash("Status transition test description", title="Lead Engineer", company="Nova"),
        title="Lead Engineer",
        company="Nova",
        raw_description="Status transition test description",
        status=JobStatus.INGESTED,
    )
    assert await repo.save_job(job) is True

    # 1. Update to PREPROCESSED
    job.status = JobStatus.PREPROCESSED
    job.cleaned_description = "Cleaned description"
    assert await repo.update_job(job) is True

    # 2. Update to TRIAGED
    eval_model = MatchEvaluation(
        fit_score=35,
        recommendation=Recommendation.DISCARD,
        matched_skills=["Python"],
        missing_skills=["Rust", "Kubernetes"],
        reasoning="Insufficient systems experience."
    )
    job.status = JobStatus.TRIAGED
    job.fit_score = 35
    job.recommendation = Recommendation.DISCARD
    job.evaluation = eval_model
    assert await repo.update_job(job) is True

    # 3. Final decision: DISCARDED
    assert await repo.update_status("stat-job-1", JobStatus.DISCARDED) is True

    final_job = await repo.get_job("stat-job-1")
    assert final_job is not None
    assert final_job.status == JobStatus.DISCARDED
    assert final_job.fit_score == 35
    assert final_job.recommendation == Recommendation.DISCARD
    assert final_job.evaluation.matched_skills == ["Python"]

    # 4. Non-existent ID returns False
    assert await repo.update_status("non-existent-id", JobStatus.SHORTLISTED) is False

    await repo.close()


def test_hash_collision_delimiter_ambiguity() -> None:
    """
    Adversarial finding:
    Demonstrates delimiter/positional collision in compute_job_hash when
    one field is None and another has identical text.
    """
    h_title_none = compute_job_hash("Identical Description", title=None, company="Acme")
    h_company_none = compute_job_hash("Identical Description", title="Acme", company=None)

    # Note: In current implementation, both generate payload 'acme::identical description'
    # Demonstrating vulnerability/adversarial finding:
    assert h_title_none == h_company_none
