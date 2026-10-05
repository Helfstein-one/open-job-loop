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
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
import typer

from src.core.truncator import TextTruncator
from src.db.repository import JobRepository
from src.llm.evaluator import JobFitEvaluator
from src.mcp.client import BaseJobIngestionClient, McpJobClient
from src.mcp.mock_client import MockMcpJobClient
from src.models.schemas import CandidateProfile, JobPosting, JobStatus, Recommendation
from src.ui.banner import DEFAULT_VERSION, render_banner
from src.ui.console import create_pipeline_ui, BasePipelineUI, UIState


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
    fixture: Optional[str] = typer.Option(
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
    # Render banner in interactive terminals
    if not headless and console.is_terminal:
        render_banner(console=console, model=model, db_path=db_path)

    start_time = time.monotonic()

    # Candidate profile setup
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

            # 1. Initialize DB Repository
            repo = JobRepository(db_path=db_path)
            await repo.initialize()

            # 2. Initialize Evaluator
            evaluator = JobFitEvaluator(
                base_url=base_url,
                model=model,
                timeout=timeout,
                score_threshold=threshold,
            )

            # 3. Initialize Truncator
            truncator = TextTruncator(max_tokens=1500)

            # 4. Initialize Ingestion Client
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

            # 5. Check if JobPipeline from src.core.pipeline is available
            try:
                from src.core.pipeline import JobPipeline
                has_external_pipeline = True
            except ImportError:
                has_external_pipeline = False

            metrics: Dict[str, int] = {
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
                if has_external_pipeline:
                    # Leverage full JobPipeline orchestrator
                    from src.core.harness import LocalLoopGuard
                    guard = LocalLoopGuard(
                        max_iterations=max_iterations,
                        timeout_seconds=timeout,
                        repository=repo,
                    )
                    pipeline = JobPipeline(
                        repository=repo,
                        evaluator=evaluator,
                        mcp_client=mcp_client,
                        truncator=truncator,
                        guard=guard,
                        candidate_profile=profile,
                        ui_listener=ui,
                    )
                    result_metrics = await pipeline.run(limit=limit)
                    metrics.update(result_metrics)
                else:
                    # Built-in direct execution harness fallback
                    ui.on_stage_update(1, "Ingestion (MCP)", "RUNNING", "Fetching jobs...")
                    raw_jobs = await mcp_client.fetch_jobs(limit=limit)
                    metrics["discovered"] = len(raw_jobs)
                    ui.on_stage_update(1, "Ingestion (MCP)", "DONE", f"Fetched {len(raw_jobs)} jobs")

                    processed_count = 0
                    for idx, job in enumerate(raw_jobs, start=1):
                        if processed_count >= limit:
                            break

                        ui.on_job_start(job, iteration=idx, max_iterations=min(limit, len(raw_jobs)))

                        # Stage 2: Deduplication
                        ui.on_stage_update(2, "Deduplication (DuckDB)", "RUNNING")
                        is_dup = await repo.is_duplicate(job.content_hash)
                        if is_dup:
                            metrics["duplicate"] += 1
                            job.status = JobStatus.DUPLICATE
                            await repo.save_job(job)
                            ui.on_stage_update(2, "Deduplication (DuckDB)", "SKIPPED", "Duplicate SHA256")
                            ui.on_job_skipped("Duplicate posting content hash", job=job)
                            continue
                        else:
                            await repo.save_job(job)
                            metrics["ingested"] += 1
                            ui.on_stage_update(2, "Deduplication (DuckDB)", "DONE")

                        # Stage 3: Pre-Processing
                        ui.on_stage_update(3, "Pre-Processing (Truncator)", "RUNNING")
                        try:
                            trunc_res = truncator.process(job.raw_description)
                            job.cleaned_description = trunc_res.cleaned_text
                            job.description = trunc_res.final_text
                            job.token_count = trunc_res.final_tokens
                            job.is_truncated = trunc_res.was_truncated
                            job.status = JobStatus.PREPROCESSED
                            await repo.update_job(job)
                            metrics["preprocessed"] += 1
                            ui.on_stage_update(3, "Pre-Processing (Truncator)", "DONE")
                        except Exception as exc:
                            job.status = JobStatus.FAILED
                            job.error_message = str(exc)
                            await repo.update_job(job)
                            metrics["error"] += 1
                            ui.on_stage_update(3, "Pre-Processing (Truncator)", "ERROR", str(exc))
                            continue

                        # Stage 4: Triage (Local LLM via Instructor)
                        ui.on_stage_update(4, "Triage (Llama 3.2)", "RUNNING", "Running local LLM evaluation...")
                        try:
                            # Direct evaluation with wall-clock timeout protection
                            async with asyncio.timeout(timeout):
                                evaluation = await evaluator.evaluate_fit(
                                    job_description=job.description or job.raw_description,
                                    candidate_profile=profile,
                                )
                                job.evaluation = evaluation
                                job.fit_score = evaluation.fit_score
                                job.recommendation = evaluation.recommendation
                                job.status = JobStatus.TRIAGED
                                await repo.update_job(job)
                                ui.on_job_triaged(job, evaluation)
                                ui.on_stage_update(4, "Triage (Llama 3.2)", "DONE")
                        except TimeoutError:
                            job.status = JobStatus.SKIPPED_TIMEOUT
                            job.error_message = f"Evaluation exceeded {timeout}s timeout"
                            await repo.update_status(job.id, JobStatus.SKIPPED_TIMEOUT, error_message=job.error_message)
                            metrics["skipped_timeout"] += 1
                            ui.on_stage_update(4, "Triage (Llama 3.2)", "TIMEOUT", "Skipped timeout")
                            ui.on_job_skipped("LLM inference timeout", job=job)
                            continue
                        except Exception as exc:
                            job.status = JobStatus.ERROR
                            job.error_message = str(exc)
                            await repo.update_status(job.id, JobStatus.ERROR, error_message=str(exc))
                            metrics["error"] += 1
                            ui.on_stage_update(4, "Triage (Llama 3.2)", "ERROR", str(exc))
                            continue

                        # Stage 5: Decision Tree
                        ui.on_stage_update(5, "Decision Tree (Flush)", "RUNNING")
                        if job.recommendation == Recommendation.SHORTLIST or (job.fit_score and job.fit_score >= threshold):
                            job.status = JobStatus.SHORTLISTED
                            metrics["shortlisted"] += 1
                        else:
                            job.status = JobStatus.DISCARDED
                            metrics["discarded"] += 1

                        await repo.update_job(job)
                        ui.on_stage_update(5, "Decision Tree (Flush)", "DONE")
                        ui.on_job_completed(job, metrics)
                        processed_count += 1

                ui.on_metrics_update(metrics)
            finally:
                await mcp_client.disconnect()
                await evaluator.close()
                await repo.close()

            # Final summary print
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
