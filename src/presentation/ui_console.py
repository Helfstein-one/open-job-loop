from __future__ import annotations
"""
Rich live console layout and telemetry dashboard for open-job-loop.

Provides:
- 5-stage DAG pipeline status tracking (Ingestion -> Deduplication -> Pre-Processing -> Triage -> Decision Tree)
- Current candidate job card display with fit score badges and skill tags
- Real-time live metrics summary (Ingested, Duplicates, Shortlisted, Discarded, Timeouts, Errors)
- Animated spinners and status indicators
- Headless / non-TTY fallback for CI, piped output, and automated testing
"""


import abc
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.domain.models import JobPosting, JobStatus, MatchEvaluation

# ==============================================================================
# Pipeline Stage Constants & UI State
# ==============================================================================

STAGE_NAMES = [
    "1. Ingestion (MCP)",
    "2. Deduplication (DuckDB)",
    "3. Pre-Processing (Truncator)",
    "4. Triage (Llama 3.2)",
    "5. Decision Tree (Flush)",
]

STAGE_ICONS = {
    "PENDING": "[dim]• Pending[/]",
    "RUNNING": "[bold cyan]⟳ Running...[/]",
    "DONE": "[bold green]✓ Done[/]",
    "SKIPPED": "[dim yellow]↷ Skipped[/]",
    "TIMEOUT": "[bold yellow]⏱ Timeout[/]",
    "ERROR": "[bold red]✗ Error[/]",
}


@dataclass
class UIState:
    """Telemetry state container for the terminal UI dashboard."""
    keywords: str = "Python Software Engineer"
    location: str = "Remote"
    limit: int = 10
    threshold: int = 70
    model: str = "llama3.2:3b"
    db_path: str = "open_job_loop.duckdb"

    current_iteration: int = 0
    max_iterations: int = 50
    start_time: float = field(default_factory=time.monotonic)

    stage_statuses: dict[int, str] = field(
        default_factory=lambda: {i: "PENDING" for i in range(1, 6)}
    )

    current_job_id: str | None = None
    current_job_title: str | None = None
    current_job_company: str | None = None
    current_job_location: str | None = None
    current_job_status: str | None = None
    current_fit_score: int | None = None
    current_recommendation: str | None = None
    current_matched_skills: list[str] = field(default_factory=list)
    current_missing_skills: list[str] = field(default_factory=list)
    current_reasoning: str | None = None
    current_tokens: int | None = None

    metrics: dict[str, int] = field(
        default_factory=lambda: {
            "discovered": 0,
            "ingested": 0,
            "duplicate": 0,
            "preprocessed": 0,
            "shortlisted": 0,
            "discarded": 0,
            "skipped_timeout": 0,
            "error": 0,
        }
    )
    status_message: str = "Initializing pipeline..."


# ==============================================================================
# Panel Builders
# ==============================================================================

def build_header_panel(state: UIState) -> Panel:
    """Build the top session status and configuration panel."""
    elapsed = int(time.monotonic() - state.start_time)
    mins, secs = divmod(elapsed, 60)
    time_str = f"{mins:02d}:{secs:02d}"

    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)

    grid.add_row(
        f"[bold cyan]Search:[/] [white]{state.keywords} ({state.location})[/]",
        f"[bold cyan]Model:[/] [bright_white]{state.model} @ Ollama[/]",
    )
    grid.add_row(
        f"[bold cyan]Target Limit:[/] [white]{state.limit}[/] | "
        f"[bold cyan]Threshold:[/] [bright_white]{state.threshold}/100[/] | "
        f"[bold cyan]Iteration:[/] [white]{state.current_iteration}/{state.max_iterations}[/]",
        f"[bold cyan]Database:[/] [dim]{state.db_path}[/] | "
        f"[bold cyan]Elapsed:[/] [bright_yellow]{time_str}[/]",
    )

    return Panel(
        grid,
        title=f"[bold bright_white] OPEN-JOB-LOOP DASHBOARD [/] — [bold green]{state.status_message}[/]",
        border_style="cyan",
        padding=(0, 1),
    )


def build_pipeline_panel(state: UIState) -> Panel:
    """Build the left panel depicting the 5 DAG pipeline stages."""
    table = Table.grid(padding=(0, 1))
    table.add_column(justify="left", style="bold")
    table.add_column(justify="left")

    for idx, name in enumerate(STAGE_NAMES, start=1):
        status_key = state.stage_statuses.get(idx, "PENDING")
        icon = STAGE_ICONS.get(status_key, "[dim]•[/]")
        name_style = "bold white" if status_key == "RUNNING" else ("white" if status_key == "DONE" else "dim")
        table.add_row(icon, f"[{name_style}]{name}[/]")

    return Panel(
        table,
        title="[bold]DAG Stages[/]",
        border_style="cyan",
        padding=(0, 1),
    )


def build_current_job_panel(state: UIState) -> Panel:
    """Build the right candidate job card panel."""
    if not state.current_job_title:
        empty_msg = Align.center(
            Text.from_markup("\n[dim italic]Waiting for job discovery & ingestion...[/]\n")
        )
        return Panel(empty_msg, title="[bold]Current Candidate Job[/]", border_style="cyan")

    lines = []

    # Title & Company
    comp_loc = f"{state.current_job_company or 'Unknown'}"
    if state.current_job_location:
        comp_loc += f" ({state.current_job_location})"
    lines.append(
        Text.from_markup(f"[bold bright_white]{state.current_job_title}[/] — [cyan]{comp_loc}[/]")
    )

    # Status & Tokens
    st_val = state.current_job_status or "INGESTED"
    st_style = "bold green" if st_val == "SHORTLISTED" else ("dim red" if st_val == "DISCARDED" else "bold yellow")
    tokens_str = f"{state.current_tokens:,}" if state.current_tokens else "N/A"
    lines.append(
        Text.from_markup(f"Status: [{st_style}]{st_val}[/] | Tokens: [cyan]{tokens_str}[/]")
    )

    # Fit Score Badge
    if state.current_fit_score is not None:
        rec = state.current_recommendation or ("SHORTLIST" if state.current_fit_score >= state.threshold else "DISCARD")
        badge_style = "bold green" if rec == "SHORTLIST" else "bold red"
        lines.append(
            Text.from_markup(
                f"Fit Score: [{badge_style}]{state.current_fit_score}/100 ([{badge_style}]{rec}[/])[/]"
            )
        )
    else:
        lines.append(Text.from_markup("Fit Score: [dim]Pending LLM triage...[/]"))

    # Matched Skills
    if state.current_matched_skills:
        skills_str = ", ".join(state.current_matched_skills[:8])
        lines.append(Text.from_markup(f"Matched Skills: [green]{skills_str}[/]"))

    # Missing Skills
    if state.current_missing_skills:
        missing_str = ", ".join(state.current_missing_skills[:6])
        lines.append(Text.from_markup(f"Missing Skills: [dim yellow]{missing_str}[/]"))

    # Reasoning excerpt
    if state.current_reasoning:
        snippet = state.current_reasoning[:120] + ("..." if len(state.current_reasoning) > 120 else "")
        lines.append(Text.from_markup(f"[italic dim]Reason: {snippet}[/]"))

    return Panel(
        Group(*lines),
        title="[bold]Current Candidate Job[/]",
        border_style="cyan",
        padding=(0, 1),
    )


def build_metrics_panel(state: UIState) -> Panel:
    """Build the bottom metrics counter bar."""
    m = state.metrics
    table = Table.grid(expand=True)
    table.add_column(justify="center")
    table.add_column(justify="center")
    table.add_column(justify="center")
    table.add_column(justify="center")
    table.add_column(justify="center")
    table.add_column(justify="center")
    table.add_column(justify="center")

    duplicates_count = m.get("duplicate", m.get("duplicates", 0))
    errors_count = m.get("error", m.get("errors", 0))

    table.add_row(
        f"[cyan]Discovered: [bold]{m.get('discovered', 0)}[/][/]",
        f"[blue]Ingested: [bold]{m.get('ingested', m.get('total_ingested', 0))}[/][/]",
        f"[yellow]Duplicates: [bold]{duplicates_count}[/][/]",
        f"[green]Shortlisted: [bold]{m.get('shortlisted', 0)}[/][/]",
        f"[red]Discarded: [bold]{m.get('discarded', 0)}[/][/]",
        f"[magenta]Timeouts: [bold]{m.get('skipped_timeout', 0)}[/][/]",
        f"[bold red]Errors: [bold]{errors_count}[/][/]",
    )

    return Panel(
        table,
        title="[bold]Triage Metrics & Counters[/]",
        border_style="blue",
        padding=(0, 1),
    )


def build_layout(state: UIState) -> Layout:
    """Assemble the top-level Rich Layout combining header, DAG body, and metrics footer."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="body", size=11),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="pipeline", ratio=1),
        Layout(name="current_job", ratio=2),
    )

    layout["header"].update(build_header_panel(state))
    layout["pipeline"].update(build_pipeline_panel(state))
    layout["current_job"].update(build_current_job_panel(state))
    layout["footer"].update(build_metrics_panel(state))

    return layout


# ==============================================================================
# UI Manager Interfaces & Implementations
# ==============================================================================

class BasePipelineUI(abc.ABC):
    """Abstract base class for pipeline UI managers."""

    def __init__(self, console: Console | None = None, state: UIState | None = None) -> None:
        self.console = console or Console()
        self.state = state or UIState()

    @abc.abstractmethod
    def start(self) -> None:
        """Initialize and start the UI display."""
        ...

    @abc.abstractmethod
    def stop(self) -> None:
        """Stop and tear down the UI display."""
        ...

    @abc.abstractmethod
    def on_stage_update(
        self,
        stage_idx: int,
        stage_name: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        """Update the status of a specific DAG stage (1 through 5)."""
        ...

    @abc.abstractmethod
    def on_job_start(self, job: JobPosting, iteration: int, max_iterations: int) -> None:
        """Update the display when a new job starts processing."""
        ...

    @abc.abstractmethod
    def on_job_triaged(self, job: JobPosting, evaluation: MatchEvaluation) -> None:
        """Update the display when a job completes LLM triage."""
        ...

    @abc.abstractmethod
    def on_job_completed(self, job: JobPosting, metrics: dict[str, int]) -> None:
        """Update the display when a job completes all pipeline stages."""
        ...

    @abc.abstractmethod
    def on_job_skipped(self, reason: str, job: JobPosting | None = None) -> None:
        """Update display when a job is skipped (duplicate, timeout, error)."""
        ...

    @abc.abstractmethod
    def on_metrics_update(self, metrics: dict[str, int]) -> None:
        """Update current metrics counters."""
        ...

    @abc.abstractmethod
    def log_event(self, message: str, level: str = "info") -> None:
        """Emit an event log line."""
        ...

    def print_summary(self, metrics: dict[str, int], duration_seconds: float) -> None:
        """Print a polished final summary table."""
        mins, secs = divmod(int(duration_seconds), 60)
        table = Table(
            title=f"Execution Completed in {mins:02d}m {secs:02d}s",
            caption="open-job-loop autonomous execution summary",
            border_style="cyan",
            header_style="bold bright_white on blue",
        )
        table.add_column("Metric", style="cyan")
        table.add_column("Count", style="bright_white", justify="right")
        table.add_column("Rate", style="yellow", justify="right")

        total = metrics.get("discovered", 0) or sum(metrics.values())
        shortlisted = metrics.get("shortlisted", 0)
        discarded = metrics.get("discarded", 0)
        triaged = shortlisted + discarded

        for key, val in metrics.items():
            rate_str = f"{(val / total * 100):.1f}%" if total > 0 else "0.0%"
            label = key.replace("_", " ").title()
            table.add_row(label, str(val), rate_str)

        table.add_section()
        shortlist_pct = f"{(shortlisted / triaged * 100):.1f}%" if triaged > 0 else "N/A"
        table.add_row("[bold green]Match Fit Rate[/]", f"[bold green]{shortlisted}/{triaged}[/]", shortlist_pct)

        self.console.print(table)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.stop()


class LivePipelineUI(BasePipelineUI):
    """
    Interactive full-screen Live UI powered by rich.live.Live.
    Auto-refreshes dynamic panels, stages, and candidate cards in place.
    """

    def __init__(self, console: Console | None = None, state: UIState | None = None) -> None:
        super().__init__(console=console, state=state)
        self._live: Live | None = None

    def start(self) -> None:
        if self._live is not None:
            return
        layout = build_layout(self.state)
        self._live = Live(
            layout,
            console=self.console,
            refresh_per_second=4,
            transient=False,
            auto_refresh=True,
        )
        self._live.start()

    def stop(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(build_layout(self.state), refresh=True)

    def on_stage_update(
        self,
        stage_idx: int,
        stage_name: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        self.state.stage_statuses[stage_idx] = status
        if detail:
            self.state.status_message = detail
        self._refresh()

    def on_job_start(self, job: JobPosting, iteration: int, max_iterations: int) -> None:
        self.state.current_iteration = iteration
        self.state.max_iterations = max_iterations
        self.state.current_job_id = job.id
        self.state.current_job_title = job.title
        self.state.current_job_company = job.company
        self.state.current_job_location = job.location
        self.state.current_job_status = job.status.value if isinstance(job.status, JobStatus) else str(job.status)
        self.state.current_fit_score = job.fit_score
        self.state.current_recommendation = job.recommendation.value if job.recommendation else None
        self.state.current_tokens = job.token_count
        self.state.status_message = f"Processing job {iteration}/{max_iterations}: {job.title[:30]}"
        self.state.stage_statuses = {i: "PENDING" for i in range(1, 6)}
        self.state.stage_statuses[1] = "DONE"
        self._refresh()

    def on_job_triaged(self, job: JobPosting, evaluation: MatchEvaluation) -> None:
        self.state.current_fit_score = evaluation.fit_score
        self.state.current_recommendation = evaluation.recommendation.value
        self.state.current_matched_skills = evaluation.matched_skills or []
        self.state.current_missing_skills = evaluation.missing_skills or []
        self.state.current_reasoning = evaluation.reasoning
        self.state.stage_statuses[4] = "DONE"
        self._refresh()

    def on_job_completed(self, job: JobPosting, metrics: dict[str, int]) -> None:
        self.state.metrics.update(metrics)
        self.state.stage_statuses[5] = "DONE"
        self.state.current_job_status = job.status.value if isinstance(job.status, JobStatus) else str(job.status)
        self._refresh()

    def on_job_skipped(self, reason: str, job: JobPosting | None = None) -> None:
        self.state.status_message = f"Skipped: {reason}"
        if job:
            self.state.current_job_status = job.status.value if isinstance(job.status, JobStatus) else str(job.status)
        self._refresh()

    def on_metrics_update(self, metrics: dict[str, int]) -> None:
        self.state.metrics.update(metrics)
        self._refresh()

    def log_event(self, message: str, level: str = "info") -> None:
        self.state.status_message = message
        self._refresh()


class HeadlessPipelineUI(BasePipelineUI):
    """
    Clean, non-interactive log-based UI for headless environments (CI, non-TTY, scripts).
    Outputs formatted timestamped logs without Rich Live rewriting.
    """

    def start(self) -> None:
        now = datetime.now(UTC).strftime("%H:%M:%S")
        self.console.print(
            f"[dim][{now}][/] [bold cyan][HEADLESS][/] OpenJobLoop started "
            f"(target: {self.state.limit} jobs, threshold: {self.state.threshold}/100)"
        )

    def stop(self) -> None:
        pass

    def on_stage_update(
        self,
        stage_idx: int,
        stage_name: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        now = datetime.now(UTC).strftime("%H:%M:%S")
        det = f" - {detail}" if detail else ""
        if status in ("RUNNING", "DONE", "TIMEOUT", "ERROR"):
            self.console.print(f"[dim][{now}][/] [cyan][STAGE {stage_idx}][/] {stage_name}: {status}{det}")

    def on_job_start(self, job: JobPosting, iteration: int, max_iterations: int) -> None:
        now = datetime.now(UTC).strftime("%H:%M:%S")
        self.console.print(
            f"[dim][{now}][/] [bold blue][JOB {iteration}/{max_iterations}][/] "
            f"Ingested '{job.title}' at '{job.company}'"
        )

    def on_job_triaged(self, job: JobPosting, evaluation: MatchEvaluation) -> None:
        now = datetime.now(UTC).strftime("%H:%M:%S")
        rec = evaluation.recommendation.value
        style = "bold green" if rec == "SHORTLIST" else "dim yellow"
        self.console.print(
            f"[dim][{now}][/] [bold magenta][TRIAGE][/] '{job.title}': "
            f"score={evaluation.fit_score}/100 -> [{style}]{rec}[/]"
        )

    def on_job_completed(self, job: JobPosting, metrics: dict[str, int]) -> None:
        self.state.metrics.update(metrics)

    def on_job_skipped(self, reason: str, job: JobPosting | None = None) -> None:
        now = datetime.now(UTC).strftime("%H:%M:%S")
        job_info = f" ('{job.title}')" if job else ""
        self.console.print(f"[dim][{now}][/] [yellow][SKIPPED][/]{job_info} {reason}")

    def on_metrics_update(self, metrics: dict[str, int]) -> None:
        self.state.metrics.update(metrics)

    def log_event(self, message: str, level: str = "info") -> None:
        now = datetime.now(UTC).strftime("%H:%M:%S")
        self.console.print(f"[dim][{now}][/] [cyan][LOG][/] {message}")


def create_pipeline_ui(
    headless: bool = False,
    console: Console | None = None,
    state: UIState | None = None,
    **kwargs: Any,
) -> BasePipelineUI:
    """
    Factory function to instantiate either LivePipelineUI or HeadlessPipelineUI
    based on the headless flag and terminal detection.

    Args:
        headless: If True, forces HeadlessPipelineUI.
        console: Optional Rich Console.
        state: Optional pre-configured UIState.
        **kwargs: Initial parameters passed to UIState if state is None.

    Returns:
        Configured BasePipelineUI instance.
    """
    c = console or Console()
    ui_state = state or UIState(**kwargs)

    # Automatically force headless if stdout is not an interactive terminal
    if headless or not c.is_terminal:
        return HeadlessPipelineUI(console=c, state=ui_state)

    return LivePipelineUI(console=c, state=ui_state)
