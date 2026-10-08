"""
Tier 1: Opaque-Box CLI Smoke & Interface Tests.

Validates:
- Typer CLI command dispatch (`banner`, `stats`, `run`, `--help`)
- Startup ASCII art banner rendering and subtitle presence
- Strict parameter and option boundary validation (ranges, negative values)
- Graceful error exits without tracebacks or crashes
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from src.presentation.cli import app
from src.infrastructure.adapters.repository import JobRepository
from src.domain.models import JobPosting, JobStatus, Recommendation

runner = CliRunner()


@pytest.fixture
def temp_db_path(tmp_path):
    """Create a temporary DuckDB database file path in tmp_path."""
    return str(tmp_path / "test_smoke.duckdb")


class TestTier1CliHelpAndInfo:
    """Verify primary CLI help displays and metadata."""

    def test_cli_help_flag_returns_zero(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "open-job-loop" in result.stdout
        assert "Autonomous, privacy-first local CLI agent" in result.stdout
        assert "banner" in result.stdout
        assert "stats" in result.stdout
        assert "run" in result.stdout

    def test_cli_no_args_shows_help(self):
        result = runner.invoke(app, [])
        # no_args_is_help=True in Typer causes exit code 0 or 2 depending on version
        assert result.exit_code in (0, 2)
        assert "Usage" in result.stdout or "open-job-loop" in result.stdout


class TestTier1BannerCommand:
    """Verify startup ASCII banner rendering across formats."""

    def test_banner_command_renders_ascii_art(self):
        result = runner.invoke(app, ["banner"])
        assert result.exit_code == 0
        # The ASCII art contains OPEN-JOB-LOOP
        assert "OPEN" in result.stdout
        assert "JOB" in result.stdout
        assert "LOOP" in result.stdout
        assert "Privacy-First Local Job Search & Triage Agent" in result.stdout

    def test_banner_plain_mode(self):
        result = runner.invoke(app, ["banner", "--plain"])
        assert result.exit_code == 0
        assert "OPEN-JOB-LOOP" in result.stdout
        assert "Privacy-First Local Job Search & Triage Agent" in result.stdout

    def test_banner_custom_version(self):
        result = runner.invoke(app, ["banner", "--plain", "--version", "9.9.9-test"])
        assert result.exit_code == 0
        assert "9.9.9-test" in result.stdout

    def test_banner_help(self):
        result = runner.invoke(app, ["banner", "--help"])
        assert result.exit_code == 0
        assert "--plain" in result.stdout
        assert "--version" in result.stdout


class TestTier1StatsCommand:
    """Verify database statistics reporting."""

    def test_stats_help(self):
        result = runner.invoke(app, ["stats", "--help"])
        assert result.exit_code == 0
        assert "--db" in result.stdout

    def test_stats_empty_database(self, temp_db_path):
        result = runner.invoke(app, ["stats", "--db", temp_db_path])
        assert result.exit_code == 0
        assert "0" in result.stdout
        assert "Job Triage Database Statistics" in result.stdout

    def test_stats_populated_database(self, temp_db_path):
        import asyncio

        async def _seed():
            repo = JobRepository(db_path=temp_db_path)
            await repo.initialize()
            job1 = JobPosting(
                id="stat-01",
                content_hash="hash-stat-01",
                title="Python Lead",
                company="Acme Corp",
                raw_description="A role requiring Python and AsyncIO.",
                status=JobStatus.SHORTLISTED,
                fit_score=85,
                recommendation=Recommendation.SHORTLIST,
            )
            job2 = JobPosting(
                id="stat-02",
                content_hash="hash-stat-02",
                title="Java Developer",
                company="Legacy Corp",
                raw_description="A role requiring Java and Spring.",
                status=JobStatus.DISCARDED,
                fit_score=20,
                recommendation=Recommendation.DISCARD,
            )
            await repo.save_job(job1)
            await repo.save_job(job2)
            await repo.close()

        asyncio.run(_seed())

        result = runner.invoke(app, ["stats", "--db", temp_db_path])
        assert result.exit_code == 0
        assert "TOTAL POSTINGS" in result.stdout
        assert "SHORTLISTED" in result.stdout
        assert "DISCARDED" in result.stdout
        assert "Shortlist Fit Rate" in result.stdout


class TestTier1RunOptionValidation:
    """Verify boundary conditions and option validations on run command."""

    def test_run_help(self):
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "--keywords" in result.stdout
        assert "--limit" in result.stdout
        assert "--threshold" in result.stdout
        assert "--mock" in result.stdout
        assert "--timeout" in result.stdout
        assert "--max-iterations" in result.stdout
        assert "--headless" in result.stdout

    def test_run_invalid_threshold_above_100(self, temp_db_path):
        result = runner.invoke(app, ["run", "--mock", "--threshold", "150", "--db", temp_db_path])
        assert result.exit_code != 0
        assert "Invalid value" in result.stderr or "Invalid value" in result.stdout or "150" in result.stdout or "150" in result.stderr

    def test_run_invalid_threshold_negative(self, temp_db_path):
        result = runner.invoke(app, ["run", "--mock", "--threshold", "-10", "--db", temp_db_path])
        assert result.exit_code != 0

    def test_run_invalid_limit_zero(self, temp_db_path):
        result = runner.invoke(app, ["run", "--mock", "--limit", "0", "--db", temp_db_path])
        assert result.exit_code != 0

    def test_run_invalid_max_iterations_zero(self, temp_db_path):
        result = runner.invoke(app, ["run", "--mock", "--max-iterations", "0", "--db", temp_db_path])
        assert result.exit_code != 0

    def test_run_invalid_timeout_zero(self, temp_db_path):
        result = runner.invoke(app, ["run", "--mock", "--timeout", "0", "--db", temp_db_path])
        assert result.exit_code != 0
