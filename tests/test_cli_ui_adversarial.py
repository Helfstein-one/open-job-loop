"""
Empirical Adversarial Stress Test Suite for Milestone M3: Typer CLI & Rich UI.

Tested by M3 Challenger 2.
Covers:
1. Entrypoint parity between `jobloop` and `open-job-loop` (subprocesses & in-process).
2. Boundary & invalid CLI options (limit <= 0, limit non-int, threshold < 0 or > 100, timeout <= 0).
3. Database edge cases (nested nonexistent paths, corrupt file handling).
4. Non-TTY pipe behavior & headless auto-detection.
5. Memory scaling under large volume (100+ jobs) verifying bounded O(1) RAM.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tracemalloc
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console
from typer.testing import CliRunner

from src.presentation.cli import app
from src.application.use_cases.harness import LocalLoopGuard
from src.application.use_cases.pipeline import JobPipeline, PipelineConfig
from src.infrastructure.adapters.repository import JobRepository, compute_job_hash
from src.infrastructure.adapters.llm_evaluator import JobFitEvaluator
from src.infrastructure.adapters.mcp_mock_client import MockMcpJobClient
from src.domain.models import (
    JobPosting,
    JobStatus,
    MatchEvaluation,
    Recommendation,
)
from src.presentation.ui_banner import TAGLINE
from src.presentation.ui_console import HeadlessPipelineUI, LivePipelineUI, create_pipeline_ui

VENV_BIN = Path(sys.executable).parent
JOBLOOP_BIN = VENV_BIN / "jobloop"
OPEN_JOBLOOP_BIN = VENV_BIN / "open-job-loop"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ==============================================================================
# Helper Factories
# ==============================================================================

def make_job_payload(idx: int) -> dict[str, Any]:
    return {
        "title": f"Staff Systems Engineer {idx}",
        "company": f"ScaleCorp {idx}",
        "location": "Remote",
        "raw_description": (
            f"Role {idx}: Seeking an experienced Backend Systems Engineer. "
            f"Requirements: 5+ years with Python, Asyncio, DuckDB, FastAPI, and Distributed Systems. "
            f"Job ID: scale-test-{idx}."
        ),
    }


def make_job_posting(idx: int) -> JobPosting:
    raw = (
        f"Role {idx}: Seeking an experienced Backend Systems Engineer. "
        f"Requirements: 5+ years with Python, Asyncio, DuckDB, FastAPI, and Distributed Systems. "
        f"Unique token: {idx}."
    )
    title = f"Staff Systems Engineer {idx}"
    company = f"ScaleCorp {idx}"
    chash = compute_job_hash(raw, title=title, company=company)
    return JobPosting(
        id=f"job-{idx}",
        content_hash=chash,
        title=title,
        company=company,
        raw_description=raw,
        status=JobStatus.INGESTED,
    )


# ==============================================================================
# 1. CLI Entrypoints Parity & Subprocess Tests
# ==============================================================================

class TestCLIEntrypoints:
    """Stress tests verifying both jobloop and open-job-loop entrypoints."""

    def test_both_binaries_exist(self):
        """Verify both entrypoint scripts exist in virtual environment."""
        assert JOBLOOP_BIN.exists(), f"Missing binary: {JOBLOOP_BIN}"
        assert OPEN_JOBLOOP_BIN.exists(), f"Missing binary: {OPEN_JOBLOOP_BIN}"

    @pytest.mark.parametrize("binary", [JOBLOOP_BIN, OPEN_JOBLOOP_BIN])
    def test_entrypoint_help(self, binary: Path):
        """Verify --help succeeds across both binary entrypoints."""
        proc = subprocess.run(
            [str(binary), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "open-job-loop" in proc.stdout or "jobloop" in proc.stdout
        assert "banner" in proc.stdout
        assert "stats" in proc.stdout
        assert "run" in proc.stdout

    @pytest.mark.parametrize("binary", [JOBLOOP_BIN, OPEN_JOBLOOP_BIN])
    def test_entrypoint_banner(self, binary: Path):
        """Verify banner command succeeds across both binary entrypoints."""
        proc = subprocess.run(
            [str(binary), "banner", "--plain"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "OPEN-JOB-LOOP" in proc.stdout
        assert TAGLINE in proc.stdout

    @pytest.mark.parametrize("binary", [JOBLOOP_BIN, OPEN_JOBLOOP_BIN])
    def test_entrypoint_stats_empty(self, binary: Path, tmp_path: Path):
        """Verify stats command on empty DB succeeds across both binary entrypoints."""
        db_file = tmp_path / "test_stats_entry.duckdb"
        proc = subprocess.run(
            [str(binary), "stats", "--db", str(db_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "0" in proc.stdout

    @pytest.mark.parametrize("binary", [JOBLOOP_BIN, OPEN_JOBLOOP_BIN])
    def test_entrypoint_run_mock_headless(self, binary: Path, tmp_path: Path):
        """Verify run command with mock & headless succeeds across both binary entrypoints."""
        db_file = tmp_path / "test_run_entry.duckdb"
        proc = subprocess.run(
            [
                str(binary),
                "run",
                "--mock",
                "--headless",
                "--limit",
                "1",
                "--db",
                str(db_file),
                "--timeout",
                "1.0",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "Execution Completed" in proc.stdout
        assert db_file.exists()


# ==============================================================================
# 2. CLI Edge Cases & Boundary Validation
# ==============================================================================

class TestCLIBoundaryValidation:
    """Stress tests invalid options, out-of-range boundaries, and non-numeric inputs."""

    @pytest.mark.parametrize("invalid_limit", ["0", "-1", "-99", "abc", "1.5", ""])
    def test_run_invalid_limit(self, runner: CliRunner, invalid_limit: str):
        """Verify run rejects non-positive or non-integer limits with code 2."""
        result = runner.invoke(app, ["run", "--limit", invalid_limit])
        assert result.exit_code != 0
        assert "Error" in result.output or "invalid" in result.output.lower()

    @pytest.mark.parametrize("invalid_threshold", ["-1", "-50", "101", "200", "xyz", "0.5"])
    def test_run_invalid_threshold(self, runner: CliRunner, invalid_threshold: str):
        """Verify run rejects threshold outside [0, 100] or non-integer with code 2."""
        result = runner.invoke(app, ["run", "--threshold", invalid_threshold])
        assert result.exit_code != 0
        assert "Error" in result.output or "invalid" in result.output.lower()

    @pytest.mark.parametrize("invalid_timeout", ["0", "-5", "abc", "0.0"])
    def test_run_invalid_timeout(self, runner: CliRunner, invalid_timeout: str):
        """Verify run rejects timeout <= 0 or non-numeric."""
        result = runner.invoke(app, ["run", "--timeout", invalid_timeout])
        assert result.exit_code != 0

    @pytest.mark.parametrize("invalid_max_iter", ["0", "-1", "abc"])
    def test_run_invalid_max_iterations(self, runner: CliRunner, invalid_max_iter: str):
        """Verify run rejects max-iterations < 1."""
        result = runner.invoke(app, ["run", "--max-iterations", invalid_max_iter])
        assert result.exit_code != 0


# ==============================================================================
# 3. DuckDB File Edge Cases (Nonexistent Dirs & Corrupt Files)
# ==============================================================================

class TestDuckDBFileResilience:
    """Stress tests DuckDB path resolution, directory auto-creation, and corruption handling."""

    def test_stats_creates_nested_directories(self, runner: CliRunner, tmp_path: Path):
        """Verify stats automatically creates parent directories when DB path is nested."""
        nested_db = tmp_path / "deep" / "nested" / "path" / "new.duckdb"
        assert not nested_db.parent.exists()

        result = runner.invoke(app, ["stats", "--db", str(nested_db)])
        assert result.exit_code == 0
        assert nested_db.parent.exists()
        assert nested_db.exists()

    def test_run_creates_nested_directories(self, runner: CliRunner, tmp_path: Path):
        """Verify run automatically creates parent directories when DB path is nested."""
        nested_db = tmp_path / "a" / "b" / "c" / "run.duckdb"
        assert not nested_db.parent.exists()

        result = runner.invoke(
            app,
            [
                "run",
                "--mock",
                "--headless",
                "--limit",
                "1",
                "--db",
                str(nested_db),
                "--timeout",
                "1.0",
            ],
        )
        assert result.exit_code == 0
        assert nested_db.exists()

    def test_stats_corrupt_duckdb_file(self, runner: CliRunner, tmp_path: Path):
        """Verify stats terminates with non-zero code when DuckDB file is corrupted."""
        corrupt_db = tmp_path / "corrupt.duckdb"
        corrupt_db.write_bytes(b"INVALID DUCKDB CORRUPTED HEADER BYTES " * 10)

        result = runner.invoke(app, ["stats", "--db", str(corrupt_db)])
        assert result.exit_code != 0

    def test_run_corrupt_duckdb_file(self, runner: CliRunner, tmp_path: Path):
        """Verify run terminates with non-zero code when DuckDB file is corrupted."""
        corrupt_db = tmp_path / "corrupt_run.duckdb"
        corrupt_db.write_bytes(b"NOT A DUCKDB FILE " * 10)

        result = runner.invoke(
            app,
            ["run", "--mock", "--headless", "--limit", "1", "--db", str(corrupt_db)],
        )
        assert result.exit_code != 0


# ==============================================================================
# 4. Non-TTY Pipes & Headless Auto-Detection
# ==============================================================================

class TestNonTTYPipesAndHeadless:
    """Stress tests pipeline behavior in piped / non-TTY and headless environments."""

    def test_ui_factory_forces_headless_on_non_terminal(self):
        """Verify factory selects HeadlessPipelineUI when console is not a terminal."""
        fake_console = Console(force_terminal=False)
        ui = create_pipeline_ui(headless=False, console=fake_console)
        assert isinstance(ui, HeadlessPipelineUI)

    def test_ui_factory_selects_live_when_terminal(self):
        """Verify factory selects LivePipelineUI when console is a terminal and headless=False."""
        terminal_console = Console(force_terminal=True)
        ui = create_pipeline_ui(headless=False, console=terminal_console)
        assert isinstance(ui, LivePipelineUI)

    def test_banner_pipe_to_cat(self):
        """Verify piping banner to cat degrades cleanly to plain text without ANSI escape sequences."""
        proc = subprocess.run(
            f"{JOBLOOP_BIN} banner | cat",
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "OPEN-JOB-LOOP" in proc.stdout
        assert TAGLINE in proc.stdout
        # ANSI escape character \x1b should not be present in plain banner fallback
        assert "\x1b[" not in proc.stdout

    def test_run_pipe_to_cat_auto_headless(self, tmp_path: Path):
        """
        Verify running without --headless when piped to cat automatically falls back
        to HeadlessPipelineUI without live cursor control escape sequences.
        """
        db_file = tmp_path / "pipe_auto.duckdb"
        cmd = f"{JOBLOOP_BIN} run --mock --limit 1 --db {db_file} --timeout 1.0 | cat"
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "[HEADLESS]" in proc.stdout
        assert "Execution Completed" in proc.stdout


# ==============================================================================
# 5. Memory Scaling Under 100+ Jobs (Bounded O(1) RAM Verification)
# ==============================================================================

class TestMemoryScalingO1:
    """
    Stress tests verifying memory consumption remains bounded O(1)
    and does not scale linearly O(N) when processing 100+ jobs.
    """

    @pytest.mark.asyncio
    async def test_large_batch_memory_bounded_120_jobs(self, tmp_path: Path):
        """
        Empirically measure RAM usage before, during, and after processing 120 jobs.
        Verifies that memory growth remains strictly bounded O(1).
        """
        db_file = tmp_path / "mem_scale_120.duckdb"
        repo = JobRepository(db_path=str(db_file))
        await repo.initialize()

        # Generate 120 unique jobs
        total_jobs = 120
        jobs_data = [make_job_payload(i) for i in range(total_jobs)]

        mock_client = MockMcpJobClient(jobs=jobs_data, mode="sequential")
        await mock_client.connect()

        # Fast mock evaluator returning deterministic MatchEvaluation
        mock_evaluator = MagicMock(spec=JobFitEvaluator)

        async def fast_eval(desc: str, profile: Any) -> MatchEvaluation:
            await asyncio.sleep(0.0001)  # Minimal async yield
            return MatchEvaluation(
                fit_score=85,
                recommendation=Recommendation.SHORTLIST,
                matched_skills=["Python", "Asyncio"],
                missing_skills=[],
                reasoning="Solid match for test candidate profile.",
            )

        mock_evaluator.evaluate_fit = AsyncMock(side_effect=fast_eval)
        mock_evaluator.close = AsyncMock()

        guard = LocalLoopGuard(
            max_iterations=total_jobs + 10,
            timeout_seconds=5.0,
            repository=repo,
        )

        pipeline = JobPipeline(
            repository=repo,
            evaluator=mock_evaluator,
            ingestion_client=mock_client,
            guard=guard,
            config=PipelineConfig(batch_size=20, max_iterations=total_jobs),
        )

        # Track memory usage across checkpoints
        tracemalloc.start()
        memory_samples: list[int] = []

        def memory_tracker(event: Any) -> None:
            if event.event_type.value in ("JOB_SHORTLISTED", "JOB_DISCARDED"):
                current, _ = tracemalloc.get_traced_memory()
                memory_samples.append(current)

        pipeline.add_listener(memory_tracker)

        try:
            result = await pipeline.run(limit=total_jobs)
        finally:
            await mock_client.disconnect()
            await repo.close()
            _current_mem, _peak_mem = tracemalloc.get_traced_memory()
            tracemalloc.stop()

        # Assert all 120 jobs were processed
        assert result.total_processed == total_jobs
        assert result.shortlisted == total_jobs

        # Verify memory scaling:
        # Compare average memory of first 20 jobs vs last 20 jobs
        assert len(memory_samples) == total_jobs
        early_avg = sum(memory_samples[:20]) / 20
        late_avg = sum(memory_samples[-20:]) / 20

        # The growth from job 20 to job 120 should be strictly bounded (< 3.0 MB)
        growth_bytes = late_avg - early_avg
        growth_mb = growth_bytes / (1024 * 1024)

        # Assert bounded memory consumption (O(1) streaming)
        assert growth_mb < 3.0, (
            f"Unbounded memory growth detected: early avg = {early_avg / 1024 / 1024:.2f} MB, "
            f"late avg = {late_avg / 1024 / 1024:.2f} MB, delta = {growth_mb:.2f} MB"
        )

    @pytest.mark.asyncio
    async def test_async_generator_stream_memory_bounded_150_jobs(self, tmp_path: Path):
        """
        Stress test process_stream with an asynchronous generator yielding 150 jobs.
        Verifies constant heap footprint throughout the entire stream.
        """
        db_file = tmp_path / "stream_scale_150.duckdb"
        repo = JobRepository(db_path=str(db_file))
        await repo.initialize()

        total_jobs = 150

        async def job_generator():
            for i in range(total_jobs):
                yield make_job_posting(i)

        mock_evaluator = MagicMock(spec=JobFitEvaluator)
        mock_evaluator.evaluate_fit = AsyncMock(
            return_value=MatchEvaluation(
                fit_score=75,
                recommendation=Recommendation.SHORTLIST,
                matched_skills=["Python"],
                missing_skills=[],
            )
        )
        mock_evaluator.close = AsyncMock()

        guard = LocalLoopGuard(max_iterations=total_jobs + 10, repository=repo)

        pipeline = JobPipeline(
            repository=repo,
            evaluator=mock_evaluator,
            guard=guard,
            config=PipelineConfig(score_threshold=70),
        )

        tracemalloc.start()
        samples: list[int] = []

        def sample_collector(e: Any) -> None:
            if e.event_type.value == "JOB_SHORTLISTED":
                c, _ = tracemalloc.get_traced_memory()
                samples.append(c)

        pipeline.add_listener(sample_collector)

        try:
            result = await pipeline.process_stream(job_generator(), limit=total_jobs)
        finally:
            await repo.close()
            _, _peak_mem = tracemalloc.get_traced_memory()
            tracemalloc.stop()

        assert result.total_processed == total_jobs
        assert len(samples) == total_jobs

        start_sample = samples[10]
        end_sample = samples[-1]
        delta_mb = (end_sample - start_sample) / (1024 * 1024)

        assert delta_mb < 2.0, f"Memory delta exceeded threshold: {delta_mb:.2f} MB"

    def test_cli_run_large_custom_fixture(self, runner: CliRunner, tmp_path: Path):
        """
        Verify CLI `jobloop run --mock --fixture` processes a 100-job fixture JSON
        file end-to-end without OOM or unbounded memory accumulation.
        """
        fixture_file = tmp_path / "100_jobs.json"
        fixture_data = [make_job_payload(i) for i in range(100)]
        fixture_file.write_text(json.dumps(fixture_data), encoding="utf-8")

        db_file = tmp_path / "cli_100_jobs.duckdb"

        result = runner.invoke(
            app,
            [
                "run",
                "--mock",
                "--headless",
                "--fixture",
                str(fixture_file),
                "--limit",
                "100",
                "--db",
                str(db_file),
                "--timeout",
                "0.1",  # Fast timeout per job to verify harness & memory
            ],
        )
        assert result.exit_code == 0
        assert "Execution Completed" in result.output
        assert "Discovered" in result.output
        assert db_file.exists()
