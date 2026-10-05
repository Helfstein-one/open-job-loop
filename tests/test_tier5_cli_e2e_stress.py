"""
Tier 5 Adversarial Stress Test Suite: CLI, Rich UI, and Full System Integration.

Tested by M4 Challenger 2 (teamwork_preview_challenger).
Covers:
1. CLI execution under SIGINT / cancellation, background execution, nested subshells.
2. Live CLI rendering under unusual terminal dimensions (e.g., 20x10, 200x50, 15x5, 300x12).
3. Mock and live fixture replay with large JSON files, missing fields, invalid types.
4. Full 5-stage pipeline throughput and memory stability under sustained load.
"""

from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console
from typer.testing import CliRunner

from src.core.harness import LocalLoopGuard
from src.core.pipeline import JobPipeline
from src.core.truncator import TextTruncator
from src.db.repository import JobRepository, compute_job_hash
from src.llm.evaluator import JobFitEvaluator
from src.mcp.client import McpPayloadError, parse_job_payload
from src.mcp.mock_client import MockMcpJobClient
from src.models.schemas import (
    CandidateProfile,
    JobPosting,
    MatchEvaluation,
    Recommendation,
)
from src.ui.console import (
    LivePipelineUI,
    UIState,
    build_layout,
)

runner = CliRunner()
VENV_BIN = Path(sys.executable).parent
JOBLOOP_BIN = VENV_BIN / "jobloop"
OPEN_JOBLOOP_BIN = VENV_BIN / "open-job-loop"


# ==============================================================================
# Helper Factories
# ==============================================================================

def make_candidate_profile() -> CandidateProfile:
    return CandidateProfile(
        name="Senior Python / Systems Architect",
        target_role="Senior Systems Architect",
        years_experience=8,
        primary_skills=["Python", "AsyncIO", "DuckDB", "MCP", "FastAPI"],
        secondary_skills=["Docker", "Linux", "Kubernetes"],
        summary="Senior Systems Architect specializing in distributed Python backends.",
    )


def generate_synthetic_job_dict(idx: int) -> dict[str, Any]:
    return {
        "id": f"synthetic-job-{idx:05d}",
        "title": f"Senior Staff Engineer {idx}",
        "company": f"Nexus Tech {idx % 20}",
        "location": "Remote",
        "raw_description": (
            f"We are hiring a Senior Staff Engineer ({idx}) to lead autonomous agent systems. "
            f"Qualifications include extensive Python 3.12, AsyncIO concurrency, DuckDB caching, "
            f"distributed tracing, and microservice architectures. "
            f"Equal Opportunity Employer. Background check required."
        ),
    }


# ==============================================================================
# 1. CLI Execution Stress: Signals, Backgrounding, Subshells
# ==============================================================================

class TestCLIExecutionStress:
    """Empirical verification of CLI process resilience under OS signals and subshells."""

    def test_cli_sigint_graceful_cancellation(self, tmp_path: Path):
        """
        Verify CLI terminates promptly upon SIGINT without deadlocking or corrupting DuckDB.
        """
        db_path = str(tmp_path / "sigint_test.duckdb")

        # Launch CLI running mock loop with multiple jobs
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "src.cli",
                "run",
                "--mock",
                "--headless",
                "--limit",
                "30",
                "--db",
                db_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Allow process to boot and start async event loop
        time.sleep(0.4)

        # Send SIGINT signal
        proc.send_signal(signal.SIGINT)

        # Must exit within 5.0 seconds
        _stdout, _stderr = proc.communicate(timeout=5.0)

        # Process should have exited with non-zero (typically -SIGINT or 130 or 1)
        assert proc.returncode is not None
        assert proc.returncode != 0

        # DuckDB database file should not be locked or corrupted
        if Path(db_path).exists():
            import duckdb
            con = duckdb.connect(db_path)
            res = con.execute("SELECT count(*) FROM job_postings").fetchall()
            con.close()
            assert isinstance(res[0][0], int)

    def test_cli_background_execution_devnull(self, tmp_path: Path):
        """
        Verify CLI runs seamlessly as a background process with detached stdin (/dev/null).
        """
        db_path = str(tmp_path / "bg_test.duckdb")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "src.cli",
                "run",
                "--mock",
                "--headless",
                "--limit",
                "1",
                "--db",
                db_path,
                "--timeout",
                "2.0",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=25.0,
        )

        assert result.returncode == 0, f"CLI stderr: {result.stderr}"
        assert "Execution Completed" in result.stdout or "OPEN-JOB-LOOP" in result.stdout

        # Verify DB wrote records
        import duckdb
        con = duckdb.connect(db_path)
        count = con.execute("SELECT count(*) FROM job_postings").fetchone()[0]
        con.close()
        assert count >= 1

    def test_cli_nested_subshell_execution(self, tmp_path: Path):
        """
        Verify CLI execution wrapped inside nested shell interpreters (bash -> sh).
        """
        db_path = str(tmp_path / "nested_subshell.duckdb")

        # Nested shell execution for stats command
        subshell_cmd = f"bash -c \"sh -c '{sys.executable} -m src.cli stats --db {db_path}'\""
        res_stats = subprocess.run(
            subshell_cmd,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
        assert res_stats.returncode == 0
        assert "contains 0 job records" in res_stats.stdout

        # Nested shell execution for banner command
        banner_cmd = f"bash -c \"sh -c '{sys.executable} -m src.cli banner --plain'\""
        res_banner = subprocess.run(
            banner_cmd,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
        assert res_banner.returncode == 0
        assert "OPEN-JOB-LOOP" in res_banner.stdout

    def test_cli_pipe_auto_detects_headless(self, tmp_path: Path):
        """
        Verify CLI automatically switches to headless mode when stdout is piped (non-TTY).
        """
        db_path = str(tmp_path / "piped.duckdb")

        # Notice --headless is omitted, but pipe redirect makes stdout non-TTY
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "src.cli",
                "run",
                "--mock",
                "--limit",
                "1",
                "--db",
                db_path,
                "--timeout",
                "2.0",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=25.0,
        )
        assert result.returncode == 0
        assert "[HEADLESS]" in result.stdout or "Execution Completed" in result.stdout

    def test_cli_parallel_subprocess_isolation(self, tmp_path: Path):
        """
        Verify multiple parallel CLI processes run isolated without resource collisions.
        """
        db1 = str(tmp_path / "parallel_1.duckdb")
        db2 = str(tmp_path / "parallel_2.duckdb")

        cmd1 = [sys.executable, "-m", "src.cli", "run", "--mock", "--headless", "--limit", "1", "--timeout", "2.0", "--db", db1]
        cmd2 = [sys.executable, "-m", "src.cli", "run", "--mock", "--headless", "--limit", "1", "--timeout", "2.0", "--db", db2]

        p1 = subprocess.Popen(cmd1, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p2 = subprocess.Popen(cmd2, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        _out1, err1 = p1.communicate(timeout=25.0)
        _out2, err2 = p2.communicate(timeout=25.0)

        assert p1.returncode == 0, f"P1 error: {err1}"
        assert p2.returncode == 0, f"P2 error: {err2}"


# ==============================================================================
# 2. Live CLI Rendering Stress: Unusual Terminal Dimensions
# ==============================================================================

class TestLiveUIRenderingStress:
    """Stress tests verifying Rich Live UI and Layout under extreme console geometries."""

    def test_live_ui_extreme_small_dimensions_20x10(self):
        """
        Test Rich Live rendering on extremely restricted 20x10 terminal.
        Must not raise Layout/sizing exceptions or negative buffer errors.
        """
        console = Console(width=20, height=10, force_terminal=True)
        ui = LivePipelineUI(console=console)

        with ui:
            ui.log_event("20x10 render test")
            ui.on_stage_update(1, "Ingestion", "DONE")
            ui.on_stage_update(2, "Dedup", "RUNNING")

            posting = JobPosting(
                id="test-cramped-01",
                content_hash="hash-cramped",
                title="Python Lead",
                company="Micro Corp",
                raw_description="Short job description for small screen.",
            )
            ui.on_job_start(posting, iteration=1, max_iterations=10)
            ui.on_job_triaged(
                posting,
                MatchEvaluation(
                    fit_score=92,
                    recommendation=Recommendation.SHORTLIST,
                    matched_skills=["Python", "AsyncIO"],
                    reasoning="Great fit despite tiny viewport.",
                ),
            )
            ui.on_job_completed(posting, {"discovered": 1, "shortlisted": 1})

        # Summary print on small console
        ui.print_summary({"discovered": 1, "shortlisted": 1}, duration_seconds=1.2)

    def test_live_ui_ultra_cramped_dimensions_15x5(self):
        """
        Test rendering on extreme micro-viewport (15 cols x 5 rows).
        """
        console = Console(width=15, height=5, force_terminal=True)
        state = UIState()
        layout = build_layout(state)

        with console.capture() as capture:
            console.print(layout)
        output = capture.get()
        assert len(output) > 0

    def test_live_ui_extreme_large_dimensions_200x50(self):
        """
        Test rendering on massive 4K/ultrawide terminal (200 cols x 50 rows).
        """
        console = Console(width=200, height=50, force_terminal=True)
        ui = LivePipelineUI(console=console)

        with ui:
            posting = JobPosting(
                id="test-huge-01",
                content_hash="hash-huge",
                title="Principal Distributed Systems & AI Agent Infrastructure Architect",
                company="HyperScale Global Distributed Cloud Technologies Inc",
                location="San Francisco, CA / Remote Worldwide",
                raw_description="Massive job description rendered across ultra-wide layout.",
            )
            ui.on_job_start(posting, iteration=5, max_iterations=50)
            ui.on_job_triaged(
                posting,
                MatchEvaluation(
                    fit_score=88,
                    recommendation=Recommendation.SHORTLIST,
                    matched_skills=[
                        "Python 3.12",
                        "AsyncIO",
                        "DuckDB",
                        "SQLModel",
                        "Instructor",
                        "Ollama",
                        "MCP Protocol",
                        "Distributed Systems",
                    ],
                    missing_skills=["Rust", "CUDA"],
                    reasoning="Candidate meets nearly all architectural criteria with exemplary mastery.",
                ),
            )
            ui.on_metrics_update({"discovered": 5, "shortlisted": 4, "discarded": 1})

        ui.print_summary({"discovered": 5, "shortlisted": 4, "discarded": 1}, duration_seconds=12.5)

    def test_live_ui_wide_short_dimensions_300x12(self):
        """
        Test ultra-wide but vertically constrained console (300 cols x 12 rows).
        """
        console = Console(width=300, height=12, force_terminal=True)
        state = UIState(limit=50, threshold=80)
        layout = build_layout(state)

        with console.capture() as capture:
            console.print(layout)
        rendered = capture.get()
        assert "OPEN-JOB-LOOP DASHBOARD" in rendered

    def test_live_ui_tall_narrow_dimensions_35x80(self):
        """
        Test tall narrow mobile-like terminal (35 cols x 80 rows).
        """
        console = Console(width=35, height=80, force_terminal=True)
        state = UIState(keywords="Data Systems Engineer", location="Remote")
        layout = build_layout(state)

        with console.capture() as capture:
            console.print(layout)
        rendered = capture.get()
        assert len(rendered) > 0


# ==============================================================================
# 3. Fixture Replay Stress: Large JSON, Missing Fields, Invalid Types
# ==============================================================================

class TestFixtureReplayStress:
    """Stress tests verifying mock client and parser behavior on adversarial payloads."""

    def test_large_fixture_replay_1000_jobs(self, tmp_path: Path):
        """
        Verify mock client loads and sequentially paginates 1,000 jobs from JSON fixture.
        """
        fixture_file = tmp_path / "large_fixture_1000.json"
        synthetic_jobs = [generate_synthetic_job_dict(i) for i in range(1000)]
        fixture_file.write_text(json.dumps(synthetic_jobs), encoding="utf-8")

        client = MockMcpJobClient(fixture_path=fixture_file, mode="sequential")
        assert client.total_jobs == 1000

        async def _fetch_all():
            await client.connect()
            fetched_total = 0
            while True:
                batch = await client.fetch_jobs(limit=100)
                if not batch:
                    break
                fetched_total += len(batch)
            await client.disconnect()
            return fetched_total

        total_fetched = asyncio.run(_fetch_all())
        assert total_fetched == 1000
        assert client.cursor == 1000

    def test_fixture_missing_title_raises_mcp_payload_error(self, tmp_path: Path):
        """
        Verify missing or empty title raises McpPayloadError with descriptive message.
        """
        fixture_file = tmp_path / "missing_title.json"
        fixture_file.write_text(
            json.dumps([{"company": "ACME", "raw_description": "Valid description here"}]),
            encoding="utf-8",
        )

        with pytest.raises(McpPayloadError, match="missing or empty 'title'"):
            MockMcpJobClient(fixture_path=fixture_file)

    def test_fixture_missing_company_raises_mcp_payload_error(self, tmp_path: Path):
        """
        Verify missing or empty company raises McpPayloadError with descriptive message.
        """
        fixture_file = tmp_path / "missing_company.json"
        fixture_file.write_text(
            json.dumps([{"title": "Staff Engineer", "raw_description": "Valid description"}]),
            encoding="utf-8",
        )

        with pytest.raises(McpPayloadError, match="missing or empty 'company'"):
            MockMcpJobClient(fixture_path=fixture_file)

    def test_fixture_missing_description_raises_mcp_payload_error(self, tmp_path: Path):
        """
        Verify missing or empty raw_description raises McpPayloadError.
        """
        fixture_file = tmp_path / "missing_desc.json"
        fixture_file.write_text(
            json.dumps([{"title": "Staff Engineer", "company": "ACME"}]),
            encoding="utf-8",
        )

        with pytest.raises(McpPayloadError, match="missing or empty 'raw_description'"):
            MockMcpJobClient(fixture_path=fixture_file)

    def test_fixture_invalid_types_rejection(self):
        """
        Verify invalid field types (non-strings, malformed containers) are strictly rejected.
        """
        # Title as integer
        with pytest.raises(McpPayloadError):
            parse_job_payload({"title": 12345, "company": "ACME", "raw_description": "Valid desc"})

        # Company as boolean
        with pytest.raises(McpPayloadError):
            parse_job_payload({"title": "Lead", "company": False, "raw_description": "Valid desc"})

        # Raw description as list
        with pytest.raises(McpPayloadError):
            parse_job_payload({"title": "Lead", "company": "ACME", "raw_description": ["not", "string"]})

        # Non-dict top level item
        with pytest.raises(McpPayloadError):
            parse_job_payload("just a string")

    def test_fixture_corrupted_json_syntax_raises(self, tmp_path: Path):
        """
        Verify unparseable syntax in fixture file raises JSONDecodeError.
        """
        bad_json = tmp_path / "bad.json"
        bad_json.write_text("{\"unclosed_key\": [1, 2,", encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            MockMcpJobClient(fixture_path=bad_json)

    def test_fixture_golden_jobs_contract_compliance(self):
        """
        Verify fixtures/golden_jobs.json loads cleanly with all 6 calibrated test postings.
        """
        golden_path = Path("fixtures/golden_jobs.json")
        assert golden_path.exists(), "fixtures/golden_jobs.json must exist"

        client = MockMcpJobClient(fixture_path=golden_path)
        assert client.total_jobs == 6
        for job in client._jobs:
            assert isinstance(job, JobPosting)
            assert len(job.id) > 0
            assert len(job.title) > 0
            assert len(job.company) > 0
            assert len(job.content_hash) == 64  # Valid SHA256 length


# ==============================================================================
# 4. Pipeline Sustained Load & Memory Stability Stress
# ==============================================================================

class TestPipelineSustainedLoadStress:
    """Stress tests verifying 5-stage pipeline throughput and O(1) memory bounds."""

    @pytest.mark.asyncio
    async def test_pipeline_sustained_load_memory_stability_100_jobs(self, tmp_path: Path):
        """
        Process 100 jobs through the full 5-stage pipeline under tracemalloc tracking.
        Assert RAM consumption remains bounded (peak delta <= 15 MB) confirming O(1) scaling.
        """
        db_path = str(tmp_path / "sustained_load.duckdb")
        repo = JobRepository(db_path=db_path)
        await repo.initialize()

        # Generate 100 distinct jobs
        jobs = [
            JobPosting(
                id=f"job-load-{i:03d}",
                content_hash=compute_job_hash(
                    f"Description for job {i} with distinct requirements and tokens.",
                    title=f"Staff Engineer {i}",
                    company=f"Company {i}",
                ),
                title=f"Staff Engineer {i}",
                company=f"Company {i}",
                raw_description=(
                    f"Requirements for Staff Engineer {i}: Python 3.12, AsyncIO, DuckDB, "
                    f"and distributed architectures. Full lifecycle ownership."
                ),
            )
            for i in range(100)
        ]

        mcp_client = MockMcpJobClient(jobs=jobs, mode="sequential")
        await mcp_client.connect()

        # Mock evaluator returning instantaneous high fit
        mock_evaluator = MagicMock(spec=JobFitEvaluator)
        mock_evaluator.evaluate_fit = AsyncMock(
            return_value=MatchEvaluation(
                fit_score=85,
                recommendation=Recommendation.SHORTLIST,
                matched_skills=["Python", "AsyncIO", "DuckDB"],
                reasoning="Strong systems background match.",
            )
        )
        mock_evaluator.close = AsyncMock()

        truncator = TextTruncator(max_tokens=1500)
        guard = LocalLoopGuard(max_iterations=120, timeout_seconds=10.0, repository=repo)
        candidate = make_candidate_profile()

        # Track memory allocations
        tracemalloc.start()

        pipeline = JobPipeline(
            repository=repo,
            evaluator=mock_evaluator,
            ingestion_client=mcp_client,
            truncator=truncator,
            guard=guard,
            candidate_profile=candidate,
            score_threshold=70,
            ui_listener=None,
        )

        result = await pipeline.run(limit=100)

        _current_mem, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Verify pipeline execution metrics
        assert result.total_ingested == 100
        assert result.shortlisted == 100
        assert result.duplicates == 0
        assert result.errors == 0

        # Memory verification: Peak memory delta must stay strictly bounded (< 15 MB)
        peak_delta_mb = peak_mem / (1024 * 1024)
        assert peak_delta_mb < 15.0, f"Peak memory grew to {peak_delta_mb:.2f} MB, exceeding O(1) bound"

        # Verify DuckDB persisted all records immediately
        stats = await repo.get_stats()
        assert stats.get("SHORTLISTED", 0) == 100
        assert stats.get("total", 0) == 100

        await mcp_client.disconnect()
        await repo.close()

    @pytest.mark.asyncio
    async def test_pipeline_sustained_throughput_burst(self, tmp_path: Path):
        """
        Verify sustained pipeline throughput exceeds target threshold (> 25 jobs/second)
        when inference bottleneck is eliminated.
        """
        db_path = str(tmp_path / "throughput_burst.duckdb")
        repo = JobRepository(db_path=db_path)
        await repo.initialize()

        job_count = 50
        jobs = [
            JobPosting(
                id=f"burst-{i:03d}",
                content_hash=f"hash-burst-{i:03d}",
                title=f"Burst Engineer {i}",
                company=f"BurstCo {i}",
                raw_description="High-throughput pipeline benchmarking posting with standard length.",
            )
            for i in range(job_count)
        ]

        mcp_client = MockMcpJobClient(jobs=jobs, mode="sequential")
        await mcp_client.connect()

        mock_evaluator = MagicMock(spec=JobFitEvaluator)
        mock_evaluator.evaluate_fit = AsyncMock(
            return_value=MatchEvaluation(
                fit_score=50,
                recommendation=Recommendation.DISCARD,
                reasoning="Generic benchmark discard.",
            )
        )
        mock_evaluator.close = AsyncMock()

        truncator = TextTruncator(max_tokens=1500)
        guard = LocalLoopGuard(max_iterations=100, timeout_seconds=5.0, repository=repo)

        pipeline = JobPipeline(
            repository=repo,
            evaluator=mock_evaluator,
            ingestion_client=mcp_client,
            truncator=truncator,
            guard=guard,
            candidate_profile=make_candidate_profile(),
            score_threshold=70,
        )

        t0 = time.perf_counter()
        result = await pipeline.run(limit=job_count)
        elapsed = time.perf_counter() - t0

        assert result.total_ingested == job_count
        assert result.discarded == job_count

        throughput = job_count / elapsed
        assert throughput >= 25.0, f"Throughput was {throughput:.2f} jobs/sec, expected >= 25.0"

        await mcp_client.disconnect()
        await repo.close()

    @pytest.mark.asyncio
    async def test_pipeline_adversarial_mixed_stream_stability(self, tmp_path: Path):
        """
        Stress-test pipeline on a chaotic stream:
        - 10 regular valid postings
        - 10 duplicate postings (identical content hash)
        - 5 postings with raw_description too short (< 50 chars)
        - 5 postings triggering wall-clock inference timeout
        """
        db_path = str(tmp_path / "mixed_chaos.duckdb")
        repo = JobRepository(db_path=db_path)
        await repo.initialize()

        stream: list[JobPosting] = []

        # 1. Ten valid distinct jobs
        for i in range(10):
            stream.append(
                JobPosting(
                    id=f"valid-{i}",
                    content_hash=f"hash-valid-{i}",
                    title=f"Valid Engineer {i}",
                    company=f"ValidCorp {i}",
                    raw_description=f"Valid job description {i} with sufficient text length to pass.",
                )
            )

        # 2. Ten duplicates (same hash as valid-0)
        for i in range(10):
            stream.append(
                JobPosting(
                    id=f"dup-{i}",
                    content_hash="hash-valid-0",  # Duplicate!
                    title="Duplicate Engineer",
                    company="ValidCorp 0",
                    raw_description="Valid job description 0 with sufficient text length to pass.",
                )
            )

        # 3. Five too short jobs (< 50 chars)
        for i in range(5):
            stream.append(
                JobPosting(
                    id=f"short-{i}",
                    content_hash=f"hash-short-{i}",
                    title=f"Short Job {i}",
                    company="ShortCorp",
                    raw_description="Too short.",  # Only 10 chars!
                )
            )

        # 4. Five slow jobs (causing timeout in evaluator)
        for i in range(5):
            stream.append(
                JobPosting(
                    id=f"slow-{i}",
                    content_hash=f"hash-slow-{i}",
                    title=f"Slow Job {i}",
                    company="SlowCorp",
                    raw_description="Slow job description with sufficient text length that hangs in triage.",
                )
            )

        mcp_client = MockMcpJobClient(jobs=stream, mode="sequential")
        await mcp_client.connect()

        # Mock evaluator: normal for valid, raises TimeoutError for slow
        mock_evaluator = MagicMock(spec=JobFitEvaluator)

        async def _mock_eval(job_desc, cand_summary):
            if "slow" in job_desc.lower() or "slowcorp" in job_desc.lower():
                raise TimeoutError("Inference simulated timeout")
            return MatchEvaluation(
                fit_score=75,
                recommendation=Recommendation.SHORTLIST,
                reasoning="Standard valid shortlist.",
            )

        mock_evaluator.evaluate_fit = AsyncMock(side_effect=_mock_eval)
        mock_evaluator.close = AsyncMock()

        truncator = TextTruncator(max_tokens=1500)
        guard = LocalLoopGuard(max_iterations=100, timeout_seconds=0.2, repository=repo)

        pipeline = JobPipeline(
            repository=repo,
            evaluator=mock_evaluator,
            ingestion_client=mcp_client,
            truncator=truncator,
            guard=guard,
            candidate_profile=make_candidate_profile(),
            score_threshold=70,
        )

        result = await pipeline.run(limit=len(stream))

        # Check telemetry counts
        assert result.total_ingested == len(stream)
        assert result.duplicates == 10
        assert result.errors == 5  # 5 short jobs failed pre-processing
        assert result.skipped_timeout == 5  # 5 slow jobs timed out
        assert result.shortlisted == 10  # 10 valid jobs shortlisted

        # Check DuckDB status summary: 20 unique jobs persisted (duplicates skipped and never stored)
        stats = await repo.get_stats()
        assert stats.get("SHORTLISTED", 0) == 10
        assert stats.get("SKIPPED_TIMEOUT", 0) == 5
        assert stats.get("FAILED", 0) == 5
        assert stats.get("total", 0) == 20

        await mcp_client.disconnect()
        await repo.close()
