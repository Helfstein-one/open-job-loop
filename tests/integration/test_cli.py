"""
Unit and integration tests for Typer CLI and Rich UI components.

Tests:
- `src.cli`: `app`, `main`, commands `banner`, `stats`, `run`
- `src.ui.banner`: ASCII art rendering, panel construction, plain fallback
- `src.ui.console`: Live and Headless dashboard telemetry, layout assembly
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from src.presentation.cli import app, main
from src.infrastructure.adapters.repository import JobRepository, compute_job_hash
from src.domain.models import JobPosting, JobStatus, MatchEvaluation, Recommendation
from src.presentation.ui_banner import (
    TAGLINE,
    get_banner_panel,
    get_banner_text,
)
from src.presentation.ui_console import (
    HeadlessPipelineUI,
    UIState,
    build_current_job_panel,
    build_header_panel,
    build_layout,
    build_metrics_panel,
    build_pipeline_panel,
    create_pipeline_ui,
)


@pytest.fixture
def runner() -> CliRunner:
    """Typer CLI test runner fixture."""
    return CliRunner()


# ==============================================================================
# CLI Entry Point & Help Tests
# ==============================================================================

def test_cli_app_callable():
    """Verify app is a Typer instance and main is callable."""
    assert callable(app)
    assert callable(main)


def test_cli_help(runner: CliRunner):
    """Verify --help displays app name, description, and subcommands."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "open-job-loop" in result.output
    assert "banner" in result.output
    assert "run" in result.output
    assert "stats" in result.output


# ==============================================================================
# Banner Command & UI Tests
# ==============================================================================

def test_cli_banner_command(runner: CliRunner):
    """Verify `jobloop banner` renders ASCII art banner and tagline."""
    result = runner.invoke(app, ["banner"])
    assert result.exit_code == 0
    assert "OPEN-JOB-LOOP" in result.output
    assert TAGLINE in result.output


def test_cli_banner_plain_option(runner: CliRunner):
    """Verify `jobloop banner --plain` renders plain ASCII without borders."""
    result = runner.invoke(app, ["banner", "--plain"])
    assert result.exit_code == 0
    assert "OPEN-JOB-LOOP" in result.output
    assert TAGLINE in result.output


def test_banner_text_helper():
    """Test get_banner_text returns raw ASCII art."""
    text = get_banner_text(version="0.1.0")
    assert "OPEN-JOB-LOOP v0.1.0" in text
    assert "___" in text
    assert TAGLINE in text


def test_banner_panel_helper():
    """Test get_banner_panel constructs a valid Rich Panel with expected metadata."""
    panel = get_banner_panel(version="0.1.0", model="llama3.2:3b", db_path="test.duckdb")
    assert "OPEN-JOB-LOOP" in str(panel.title)
    assert TAGLINE in str(panel.subtitle)


# ==============================================================================
# Stats Command Tests
# ==============================================================================

def test_cli_stats_empty_db(runner: CliRunner, tmp_path: Path):
    """Verify `jobloop stats` on an empty database displays 0 records notice."""
    db_file = tmp_path / "empty.duckdb"
    result = runner.invoke(app, ["stats", "--db", str(db_file)])
    assert result.exit_code == 0
    assert "0" in result.output
    assert "contains" in result.output or "Job Triage Database" in result.output


@pytest.mark.asyncio
async def test_cli_stats_populated_db(runner: CliRunner, tmp_path: Path):
    """Verify `jobloop stats` on a populated database displays correct status breakdown."""
    db_file = tmp_path / "populated.duckdb"
    repo = JobRepository(db_path=str(db_file))
    await repo.initialize()

    # Seed jobs
    job1 = JobPosting(
        content_hash=compute_job_hash("desc 1", "Python Lead", "TechCorp"),
        title="Python Lead",
        company="TechCorp",
        raw_description="desc 1",
        status=JobStatus.SHORTLISTED,
        fit_score=85,
        recommendation=Recommendation.SHORTLIST,
    )
    job2 = JobPosting(
        content_hash=compute_job_hash("desc 2", "Frontend Dev", "DesignLab"),
        title="Frontend Dev",
        company="DesignLab",
        raw_description="desc 2",
        status=JobStatus.DISCARDED,
        fit_score=40,
        recommendation=Recommendation.DISCARD,
    )
    await repo.save_job(job1)
    await repo.save_job(job2)
    await repo.close()

    result = runner.invoke(app, ["stats", "--db", str(db_file)])
    assert result.exit_code == 0
    assert "SHORTLISTED" in result.output
    assert "DISCARDED" in result.output
    assert "TOTAL POSTINGS" in result.output
    assert "2" in result.output


# ==============================================================================
# Run Command Tests
# ==============================================================================

def test_cli_run_invalid_limit(runner: CliRunner):
    """Verify `jobloop run` rejects non-positive limit."""
    result = runner.invoke(app, ["run", "--limit", "0"])
    assert result.exit_code != 0


def test_cli_run_invalid_threshold(runner: CliRunner):
    """Verify `jobloop run` rejects threshold outside 0-100 range."""
    result = runner.invoke(app, ["run", "--threshold", "150"])
    assert result.exit_code != 0


def test_cli_run_mock_headless(runner: CliRunner, tmp_path: Path):
    """
    Verify `jobloop run --mock --headless` processes jobs through all stages,
    gracefully handles timeouts/mock inference, persists to DuckDB, and exits 0.
    """
    db_file = tmp_path / "test_run.duckdb"
    result = runner.invoke(
        app,
        [
            "run",
            "--mock",
            "--headless",
            "--limit",
            "1",
            "--db",
            str(db_file),
            "--timeout",
            "1.5",
        ],
    )
    assert result.exit_code == 0
    assert "HEADLESS" in result.output
    assert "Ingestion" in result.output
    assert "Execution Completed" in result.output
    assert db_file.exists()


def test_cli_run_custom_fixture(runner: CliRunner, tmp_path: Path):
    """Verify `jobloop run --mock` with custom JSON fixture file."""
    fixture_data = [
        {
            "title": "Custom Test Backend Engineer",
            "company": "FixtureCo",
            "location": "Remote",
            "raw_description": (
                "Custom job description for testing CLI fixture option. "
                "Requires extensive Python and async programming experience."
            ),
        }
    ]
    fixture_path = tmp_path / "custom_jobs.json"
    fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

    db_file = tmp_path / "fixture_run.duckdb"
    result = runner.invoke(
        app,
        [
            "run",
            "--mock",
            "--headless",
            "--fixture",
            str(fixture_path),
            "--limit",
            "1",
            "--db",
            str(db_file),
            "--timeout",
            "1.5",
        ],
    )
    assert result.exit_code == 0
    assert "Custom Test Backend Engineer" in result.output


# ==============================================================================
# UI Module Direct Tests
# ==============================================================================

def test_ui_factory_selection():
    """Verify create_pipeline_ui selects correct UI manager."""
    headless_ui = create_pipeline_ui(headless=True)
    assert isinstance(headless_ui, HeadlessPipelineUI)


def test_headless_ui_lifecycle():
    """Verify HeadlessPipelineUI emits formatted logs without raising errors."""
    ui = HeadlessPipelineUI(state=UIState(limit=5, threshold=75))
    ui.start()
    ui.on_stage_update(1, "Ingestion (MCP)", "RUNNING", "Fetching...")
    ui.on_stage_update(1, "Ingestion (MCP)", "DONE")

    job = JobPosting(
        content_hash="test_hash_123",
        title="Test Staff Engineer",
        company="Acme Corp",
        raw_description="Detailed test description text.",
    )
    ui.on_job_start(job, iteration=1, max_iterations=5)

    evaluation = MatchEvaluation(
        fit_score=90,
        recommendation=Recommendation.SHORTLIST,
        matched_skills=["Python", "FastAPI"],
        missing_skills=[],
        reasoning="Strong technical match.",
    )
    ui.on_job_triaged(job, evaluation)
    ui.on_job_skipped("Duplicate content hash", job=job)
    ui.log_event("Custom test telemetry event")
    ui.print_summary({"discovered": 5, "shortlisted": 2, "discarded": 3}, duration_seconds=10.0)
    ui.stop()


def test_live_ui_layout_generation():
    """Verify Live UI layout components build properly with populated state."""
    state = UIState(
        keywords="Go / Python Engineer",
        location="Remote",
        limit=5,
        threshold=70,
        current_job_title="Lead Architect",
        current_job_company="Alpha Inc",
        current_fit_score=82,
        current_recommendation="SHORTLIST",
        current_matched_skills=["Python", "DuckDB"],
        current_missing_skills=["Kubernetes"],
        current_reasoning="Strong profile match.",
        metrics={"discovered": 5, "shortlisted": 2, "discarded": 3},
    )

    header = build_header_panel(state)
    console = Console()
    with console.capture() as capture:
        console.print(header)
    assert "Go / Python Engineer" in capture.get()

    pipeline = build_pipeline_panel(state)
    assert "DAG Stages" in str(pipeline.title)

    job_panel = build_current_job_panel(state)
    with console.capture() as capture:
        console.print(job_panel)
    assert "Lead Architect" in capture.get()

    metrics_panel = build_metrics_panel(state)
    assert "Triage Metrics" in str(metrics_panel.title)

    layout = build_layout(state)
    assert layout["header"] is not None
    assert layout["pipeline"] is not None
    assert layout["current_job"] is not None
    assert layout["footer"] is not None
