"""
Typer command-line interface for open-job-loop.

Provides CLI entry points for:
- `jobloop` and `open-job-loop`
- Commands:
  - `banner`: render startup ASCII art banner
  - `run`: execute autonomous closed loop (MCP -> Dedup -> Triage -> Decision Tree)
  - `stats`: display database summary metrics and fit rates
- Full headless support (--headless) for CI and non-interactive environments
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.core.harness import LocalLoopGuard
from src.core.pipeline import JobPipeline
from src.core.truncator import TextTruncator
from src.db.repository import JobRepository
from src.llm.evaluator import JobFitEvaluator
from src.mcp.client import BaseJobIngestionClient, McpJobClient
from src.mcp.mock_client import MockMcpJobClient
from src.models.schemas import CandidateProfile
from src.ui.banner import DEFAULT_VERSION, render_banner
from src.ui.console import create_pipeline_ui

# ==============================================================================
# Typer Application Setup
# ==============================================================================

app = typer.Typer(
    name="open-job-loop",
    help="Autonomous, privacy-first local CLI agent for job discovery, deduplication, and triage.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

console = Console()


def run_sync(coro: Any) -> Any:
    """
    Execute an async coroutine synchronously.
    Handles nested event loops (e.g., inside pytest-asyncio test runners) safely
    by offloading to a thread pool executor.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


# ==============================================================================
# Built-in Default Fixture Jobs (Fallback if no fixture file provided in --mock)
# ==============================================================================

BUILTIN_MOCK_JOBS = [
    {
        "title": "Senior Python Backend Engineer",
        "company": "DataStream Labs",
        "location": "Remote",
        "raw_description": (
            "We are seeking a Senior Python Backend Engineer to build high-throughput data processing pipelines. "
            "Required Qualifications:\n"
            "- 5+ years professional experience with Python 3.10+\n"
            "- Deep expertise with FastAPI, Asyncio, and modern async architectures\n"
            "- Strong experience with DuckDB, SQLModel, PostgreSQL, and data modeling\n"
            "- Experience building REST APIs and microservices\n"
            "Equal Opportunity Employer. All qualified applicants will receive consideration without regard to race."
        ),
    },
    {
        "title": "Principal Distributed Systems Engineer",
        "company": "CloudNative Tech",
        "location": "Remote",
        "raw_description": (
            "Looking for a Principal Distributed Systems Engineer to scale our autonomous agent infrastructure. "
            "Requirements:\n"
            "- Extensive Python systems programming and concurrency (Asyncio, multiprocessing)\n"
            "- Proven experience with local LLM inference engines (Ollama, vLLM, llama.cpp)\n"
            "- Strong architectural skills in DAG workflows, circuit breakers, and event-driven systems\n"
            "Pre-employment background check required."
        ),
    },
    {
        "title": "Junior Frontend Web Developer",
        "company": "PixelCraft UI",
        "location": "New York, NY",
        "raw_description": (
            "Entry level Frontend Web Developer to maintain company marketing websites. "
            "Requirements:\n"
            "- HTML, CSS, JavaScript, and React basics\n"
            "- Figma design system translation\n"
            "- No Python or backend experience required."
        ),
    },
]


# ==============================================================================
# CLI Commands
# ==============================================================================

@app.command(name="banner")
def banner_cmd(
    plain: bool = typer.Option(
        False,
        "--plain",
        help="Render banner in plain unstyled ASCII text (suitable for pipes/logs).",
    ),
    version: str = typer.Option(
        DEFAULT_VERSION,
        "--version",
        help="Display specific version string in banner header.",
    ),
) -> None:
    """
    Render the startup ASCII art banner and system configuration panel.
    """
    render_banner(console=console, version=version, plain=plain)


@app.command(name="stats")
def stats_cmd(
    db_path: str = typer.Option(
        "open_job_loop.duckdb",
        "--db",
        help="Path to the DuckDB database file.",
    ),
) -> None:
    """
    Display summary statistics and triage fit rates from the DuckDB repository.
    """
    async def _fetch_and_render() -> None:
        repo = JobRepository(db_path=db_path)
        try:
            await repo.initialize()
            stats = await repo.get_stats()
        finally:
            await repo.close()

        total = stats.get("total", 0)

        if total == 0:
            console.print(
                Panel(
                    f"[dim]DuckDB database at '[bright_white]{db_path}[/]' contains [bold]0[/] job records.\n"
                    f"Run '[cyan]jobloop run[/]' to discover, deduplicate, and triage jobs.[/]",
                    title="[bold cyan]Job Triage Database Statistics[/]",
                    border_style="cyan",
                )
            )
            return

        table = Table(
            title=f"Job Triage Database Statistics ({db_path})",
            caption="open-job-loop DuckDB repository",
            border_style="cyan",
            header_style="bold bright_white on blue",
        )
        table.add_column("Status Category", style="cyan", justify="left")
        table.add_column("Count", style="bright_white", justify="right")
        table.add_column("Percentage", style="yellow", justify="right")

        STATUS_COLORS = {
            "SHORTLISTED": "bold green",
            "DISCARDED": "dim red",
            "DUPLICATE": "yellow",
            "INGESTED": "cyan",
            "PREPROCESSED": "blue",
            "TRIAGED": "magenta",
            "SKIPPED_TIMEOUT": "bold magenta",
            "ERROR": "bold red",
            "FAILED": "bold red",
        }

        for key, val in stats.items():
            if key == "total":
                continue
            pct = f"{(val / total * 100):.1f}%" if total > 0 else "0.0%"
            style = STATUS_COLORS.get(key, "white")
            table.add_row(f"[{style}]{key}[/]", str(val), pct)

        table.add_section()
        table.add_row("[bold white]TOTAL POSTINGS[/]", f"[bold white]{total}[/]", "100.0%")

        shortlisted = stats.get("SHORTLISTED", 0)
        discarded = stats.get("DISCARDED", 0)
        triaged = shortlisted + discarded
        if triaged > 0:
            rate = (shortlisted / triaged) * 100
            table.add_row(
                "[bold green]Shortlist Fit Rate[/]",
                f"[bold green]{shortlisted}/{triaged}[/]",
                f"[bold green]{rate:.1f}%[/]",
            )

        console.print(table)

    run_sync(_fetch_and_render())


@app.command(name="run")
def run_cmd(
    keywords: str = typer.Option(
        "Python Software Engineer",
        "--keywords",
        "-k",
        help="Job search query keywords.",
    ),
    location: str = typer.Option(
        "Remote",
        "--location",
        "-l",
        help="Target job location or remote preference.",
    ),
    limit: int = typer.Option(
        10,
        "--limit",
        "-n",
        min=1,
        help="Maximum number of job postings to process.",
    ),
    threshold: int = typer.Option(
        70,
        "--threshold",
        "-t",
        min=0,
        max=100,
        help="Minimum candidate fit score (0-100) for SHORTLIST recommendation.",
    ),
    mock: bool = typer.Option(
        False,
        "--mock/--no-mock",
        help="Use deterministic local mock MCP client instead of live server.",
    ),
    fixture: str | None = typer.Option(
        None,
        "--fixture",
        help="Path to JSON file containing mock job postings.",
    ),
    db_path: str = typer.Option(
        "open_job_loop.duckdb",
        "--db",
        help="DuckDB database file path for immediate persistence.",
    ),
    timeout: float = typer.Option(
        30.0,
        "--timeout",
        min=0.1,
        help="Wall-clock inference timeout in seconds per job.",
    ),
    max_iterations: int = typer.Option(
        50,
        "--max-iterations",
        min=1,
        help="Maximum loop iterations bound for the execution harness.",
    ),
    model: str = typer.Option(
        "llama3.2:3b",
        "--model",
        help="Ollama local LLM model tag for triage evaluation.",
    ),
    base_url: str = typer.Option(
        "http://localhost:11434/v1",
        "--base-url",
        help="Base URL for local OpenAI-compatible endpoint.",
    ),
    headless: bool = typer.Option(
        False,
        "--headless/--no-headless",
        help="Disable interactive live layout and use clean log streaming.",
    ),
    candidate_name: str = typer.Option(
        "Senior Python Engineer",
        "--candidate-name",
        help="Candidate profile name.",
    ),
    candidate_summary: str = typer.Option(
        "Senior Python Engineer with 7+ years building distributed backend systems, FastAPI, DuckDB, Asyncio, and LLM agent tooling.",
        "--candidate-summary",
        help="Candidate background summary.",
    ),
) -> None:
    """
    Execute the autonomous closed-loop pipeline (Discover -> Dedup -> Truncate -> Triage -> Decision Tree).
    """
    if not headless and console.is_terminal:
        render_banner(console=console, model=model, db_path=db_path)

    start_time = time.monotonic()

    profile = CandidateProfile(
        name=candidate_name,
        target_role=keywords,
        years_experience=7,
        primary_skills=["Python", "FastAPI", "Asyncio", "DuckDB", "SQLModel"],
        secondary_skills=["Docker", "Ollama", "Instructor", "PostgreSQL"],
        summary=candidate_summary,
    )

    async def _execute_loop() -> int:
        ui = create_pipeline_ui(
            headless=headless,
            console=console,
            keywords=keywords,
            location=location,
            limit=limit,
            threshold=threshold,
            model=model,
            db_path=db_path,
            max_iterations=max_iterations,
        )

        with ui:
            ui.log_event("Initializing DuckDB repository and local LLM evaluator...")

            repo = JobRepository(db_path=db_path)
            await repo.initialize()

            evaluator = JobFitEvaluator(
                base_url=base_url,
                model=model,
                timeout=timeout,
                score_threshold=threshold,
            )

            truncator = TextTruncator(max_tokens=1500)

            mcp_client: BaseJobIngestionClient
            if mock:
                if fixture and Path(fixture).exists():
                    mcp_client = MockMcpJobClient(fixture_path=fixture)
                elif Path("fixtures/golden_jobs.json").exists():
                    mcp_client = MockMcpJobClient(fixture_path="fixtures/golden_jobs.json")
                else:
                    mcp_client = MockMcpJobClient(jobs=BUILTIN_MOCK_JOBS)
            else:
                mcp_client = McpJobClient(command="uvx", args=["mcp-server-linkedin@latest"])

            await mcp_client.connect()

            guard = LocalLoopGuard(
                max_iterations=max_iterations,
                timeout_seconds=timeout,
                repository=repo,
            )

            metrics: dict[str, int] = {
                "discovered": 0,
                "ingested": 0,
                "duplicate": 0,
                "preprocessed": 0,
                "shortlisted": 0,
                "discarded": 0,
                "skipped_timeout": 0,
                "error": 0,
            }

            try:
                pipeline = JobPipeline(
                    repository=repo,
                    evaluator=evaluator,
                    ingestion_client=mcp_client,
                    truncator=truncator,
                    guard=guard,
                    candidate_profile=profile,
                    score_threshold=threshold,
                    ui_listener=ui,
                )
                result = await pipeline.run(limit=limit)
                metrics.update(result.to_dict())
                ui.on_metrics_update(metrics)
            finally:
                await mcp_client.disconnect()
                await evaluator.close()
                await repo.close()

            total_duration = time.monotonic() - start_time
            ui.print_summary(metrics, duration_seconds=total_duration)

        return 0

    exit_code = run_sync(_execute_loop())
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


# ==============================================================================
# Script Entry Point
# ==============================================================================

def main() -> None:
    """Console script entrypoint for open-job-loop and jobloop."""
    app()


if __name__ == "__main__":
    main()
