from __future__ import annotations

import os
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

import asyncio
import time
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt

from src.application.use_cases.harness import LocalLoopGuard
from src.application.use_cases.pipeline import JobPipeline
from src.application.use_cases.truncator import TextTruncator
from src.infrastructure.adapters.repository import JobRepository
from src.infrastructure.adapters.llm_evaluator import JobFitEvaluator
from src.infrastructure.adapters.cascade_evaluator import (
    CascadeJobEvaluator,
    DEFAULT_FAST_MODEL,
    DEFAULT_DEEP_MODEL,
    DEEP_HIGH_PRECISION_MODEL,
)
from src.infrastructure.adapters.mcp_client import BaseJobIngestionClient, McpJobClient
from src.infrastructure.adapters.mcp_mock_client import MockMcpJobClient
from src.domain.models import CandidateProfile
from src.application.use_cases.profile import ProfileManager
from src.application.use_cases.auto_apply import AutoApplyWorker
from src.application.use_cases.onboard import ProfilerAgent
from src.presentation.ui_banner import DEFAULT_VERSION, render_banner
from src.presentation.ui_console import create_pipeline_ui

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
        "data/open_job_loop.duckdb",
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


@app.command(name="login")
def login_cmd(
    state_file: str = typer.Option(
        "data/linkedin_state.json",
        "--state-file",
        "-s",
        help="Path to save the authenticated LinkedIn session cookies.",
    ),
) -> None:
    """
    Open an interactive browser window to log into LinkedIn and save authenticated session cookies.
    """
    async def _login() -> None:
        from playwright.async_api import async_playwright
        console.print(
            Panel(
                "[bold cyan]Iniciando navegador para login no LinkedIn...[/]\n\n"
                "1. Insira seu e-mail, senha e 2FA na janela do navegador que acabou de abrir.\n"
                "2. Assim que carregar o Feed do LinkedIn, volte a este terminal e pressione ENTER.\n"
                "3. A sessão será salva para que o robô possa buscar vagas e preencher candidaturas.",
                title="[bold yellow]Autenticação Interativa LinkedIn[/]",
                border_style="yellow",
            )
        )
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto("https://www.linkedin.com/login")

            await asyncio.to_thread(input, "\n👉 Pressione ENTER aqui após concluir o login no navegador: ")

            Path(state_file).parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=state_file)
            await browser.close()

        console.print(
            Panel(
                f"[bold green]Sessão salva com sucesso em '[bright_white]{state_file}[/]'.[/]\n"
                "O robô agora está autenticado para buscar vagas reais e executar o Easy Apply.",
                title="[bold green]Autenticação Concluída[/]",
                border_style="green",
            )
        )

    run_sync(_login())


@app.command(name="onboard")
def onboard_cmd(
    url: Optional[str] = typer.Option(
        None,
        "--url",
        "-u",
        help="LinkedIn profile URL to scrape (e.g. https://www.linkedin.com/in/username).",
    ),
    pdf: Optional[str] = typer.Option(
        None,
        "--pdf",
        "-p",
        help="Path to LinkedIn profile PDF export.",
    ),
    state_file: str = typer.Option(
        "data/linkedin_state.json",
        "--state-file",
        "-s",
        help="Path to saved LinkedIn cookies session file.",
    ),
    db_path: str = typer.Option(
        "data/open_job_loop.duckdb",
        "--db",
        help="DuckDB database path.",
    ),
    profile_path: str = typer.Option(
        "data/profile.yaml",
        "--output",
        "-o",
        help="Target YAML path to save compiled candidate profile.",
    ),
    model: str = typer.Option(
        "llama3.2:3b",
        "--model",
        help="Ollama LLM model to analyze profile and generate adaptive questions.",
    ),
    mock: bool = typer.Option(
        False,
        "--mock/--no-mock",
        help="Use deterministic mock reasoning and profile extraction.",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Accept suggested defaults without prompting in terminal.",
    ),
) -> None:
    """
    Onboard a LinkedIn profile: analyze background, formulate target job profile,
    conduct adaptive grill-me interview, and compile profile.yaml into DuckDB VSS.
    """
    async def _onboard() -> None:
        agent = ProfilerAgent(model=model, db_path=db_path, mock_mode=mock)
        raw_text = ""

        # 1. Extraction
        if pdf:
            console.print(f"[cyan]Lendo perfil a partir do PDF:[/] [bright_white]{pdf}[/]")
            raw_text = agent.extract_from_pdf(pdf)
        elif url:
            console.print(f"[cyan]Navegando até o perfil no LinkedIn:[/] [bright_white]{url}[/]")
            with console.status("[cyan]Acessando LinkedIn e extraindo dados do perfil...[/]"):
                raw_text = await agent.extract_from_url(url, state_file=state_file)
        else:
            if not mock and console.is_terminal and not non_interactive:
                choice = Prompt.ask(
                    "[bold yellow]Como deseja fornecer seu perfil?[/]\n"
                    "Digite a [bold cyan]URL do LinkedIn[/] ou o [bold cyan]caminho de um PDF[/]",
                    default="data/profile.yaml",
                )
                if choice.endswith(".pdf"):
                    raw_text = agent.extract_from_pdf(choice)
                elif choice.startswith("http"):
                    with console.status("[cyan]Acessando LinkedIn e extraindo dados do perfil...[/]"):
                        raw_text = await agent.extract_from_url(choice, state_file=state_file)
                else:
                    raw_text = ""

        # 2. Analysis & Target Job Profile Formulation
        with console.status("[cyan]Profiler Agent analisando perfil e formulando Perfil Alvo...[/]"):
            analysis = await agent.analyze_profile(raw_text)

        cand = analysis.get("candidate", {})
        target = analysis.get("target_profile", {})

        roles_str = ", ".join(target.get("target_roles", []))
        strengths_str = " • " + "\n • ".join(target.get("key_strengths", []))

        console.print(
            Panel(
                f"[bold green]Candidato:[/] [bright_white]{cand.get('name', 'N/A')}[/] ({cand.get('title', 'N/A')})\n"
                f"[bold green]Senioridade Inquirida:[/] {target.get('seniority_level', 'Senior')}\n"
                f"[bold green]Cargos Alvo Sugeridos:[/] [bold cyan]{roles_str}[/]\n"
                f"[bold green]Palavras-chave de Busca:[/] [yellow]{target.get('suggested_keywords', '')}[/]\n"
                f"[bold green]Benchmark Salarial:[/] [magenta]{target.get('estimated_salary_range', '')}[/]\n\n"
                f"[bold underline]Pontos Fortes Identificados:[/]\n{strengths_str}",
                title="[bold cyan]🎯 Perfil Alvo de Vagas Proposto (Target Job Spec)[/]",
                border_style="cyan",
            )
        )

        # 3. Entrevista Adaptativa (Grill-Me)
        console.print("\n[bold yellow]🎙️  Iniciando Entrevista Adaptativa (Grill-Me) para Calibração de Preferências[/]")
        console.print("[dim]Pressione ENTER para aceitar a sugestão do robô ou digite sua resposta personalizada.[/]\n")

        answers: Dict[str, str] = {}
        questions = target.get("suggested_questions", [])

        for idx, q_item in enumerate(questions, 1):
            q_id = q_item.get("id", f"q_{idx}")
            q_text = q_item.get("question", "")
            default_val = q_item.get("default", "")

            if non_interactive or mock or not console.is_terminal:
                answers[q_id] = default_val
                console.print(f"[bold cyan][{idx}/{len(questions)}][/] {q_text}")
                console.print(f"👉 [green]Resposta adotada:[/] {default_val}\n")
            else:
                user_ans = Prompt.ask(f"[bold cyan][{idx}/{len(questions)}][/] {q_text}", default=default_val)
                answers[q_id] = user_ans

        # 4. Compilação e Persistência
        profile_data = agent.compile_profile_yaml(analysis, answers, output_path=profile_path)

        # 5. Vetorização no DuckDB VSS
        manager = ProfileManager(db_path=db_path, mock_mode=mock)
        with console.status(f"[cyan]Vetorizando novo perfil em {db_path} via DuckDB VSS...[/]"):
            count = await manager.sync_from_yaml(profile_path)

        console.print(
            Panel(
                f"[bold green]✅ Onboarding Concluído com Sucesso![/]\n"
                f"• Perfil consolidado em: [bright_white]{profile_path}[/]\n"
                f"• Blocos indexados no DuckDB VSS: [bold cyan]{count}[/]\n"
                f"• Palavra-chave para busca: [yellow]{profile_data['target_job_profile']['search_keywords']}[/]\n\n"
                f"[bold underline]Próximo Passo Recomendado:[/] Execute a busca e o Auto-Apply com multi-agentes:\n"
                f"[bright_white]python -m src.presentation.cli run --no-mock --keywords \"{profile_data['target_job_profile']['search_keywords']}\" --apply --dry-run[/]",
                title="[bold green]Multi-Agent Onboarding Finalizado[/]",
                border_style="green",
            )
        )

    run_sync(_onboard())


# ==============================================================================
# Profile Management Subcommands
# ==============================================================================

profile_app = typer.Typer(
    name="profile",
    help="Manage candidate patterns, profile YAML, and DuckDB VSS vector embeddings.",
    no_args_is_help=True,
)
app.add_typer(profile_app, name="profile")


@profile_app.command(name="sync")
def profile_sync_cmd(
    file: str = typer.Option(
        "data/profile.yaml",
        "--file",
        "-f",
        help="Path to the candidate profile YAML file.",
    ),
    db_path: str = typer.Option(
        "data/open_job_loop.duckdb",
        "--db",
        help="Path to the DuckDB database file.",
    ),
    mock: bool = typer.Option(
        False,
        "--mock/--no-mock",
        help="Use deterministic mock embeddings (useful for tests or without Ollama).",
    ),
) -> None:
    """
    Sync and vectorize candidate profile into DuckDB VSS for RAG form filling.
    """
    async def _sync() -> None:
        manager = ProfileManager(db_path=db_path, mock_mode=mock)
        with console.status(f"[cyan]Vectorizing profile from '{file}' into DuckDB VSS...[/]"):
            count = await manager.sync_from_yaml(file)
        console.print(
            Panel(
                f"[bold green]Successfully vectorized and indexed {count} semantic chunks[/] into DuckDB VSS.\n"
                f"Database: [bright_white]{db_path}[/]\n"
                f"Source: [bright_white]{file}[/]",
                title="[bold cyan]Profile Synchronization[/]",
                border_style="green",
            )
        )

    run_sync(_sync())


@profile_app.command(name="show")
def profile_show_cmd(
    db_path: str = typer.Option(
        "data/open_job_loop.duckdb",
        "--db",
        help="Path to the DuckDB database file.",
    ),
) -> None:
    """
    List candidate profile categories and semantic chunks stored in DuckDB VSS.
    """
    manager = ProfileManager(db_path=db_path)
    chunks = manager.list_chunks()
    if not chunks:
        console.print(f"[yellow]No profile chunks found in '{db_path}'. Run 'jobloop profile sync' first.[/]")
        return

    table = Table(
        title=f"Stored Candidate Profile Patterns ({db_path})",
        border_style="cyan",
        header_style="bold bright_white on blue",
    )
    table.add_column("Category", style="cyan", justify="left")
    table.add_column("Content Preview", style="white", justify="left")

    for ch in chunks:
        preview = ch["content"].replace("\n", " ")
        if len(preview) > 100:
            preview = preview[:97] + "..."
        table.add_row(ch["category"], preview)

    console.print(table)


@profile_app.command(name="ask")
def profile_ask_cmd(
    question: str = typer.Argument(..., help="Question to test against candidate profile RAG."),
    db_path: str = typer.Option(
        "data/open_job_loop.duckdb",
        "--db",
        help="Path to the DuckDB database file.",
    ),
    mock: bool = typer.Option(
        False,
        "--mock/--no-mock",
        help="Use mock reasoning / heuristics instead of live Ollama.",
    ),
) -> None:
    """
    Query candidate profile RAG and test synthesized form answers.
    """
    async def _ask() -> None:
        manager = ProfileManager(db_path=db_path, mock_mode=mock)
        with console.status(f"[cyan]Searching profile and synthesizing answer for: '{question}'...[/]"):
            matches = await manager.search_profile(question, limit=2)
            answer = await manager.synthesize_answer(question)

        console.print(f"\n[bold cyan]Question:[/] {question}")
        console.print(f"[bold green]Synthesized Answer:[/] [bold bright_white]{answer}[/]\n")

        table = Table(title="Top Retrieved RAG Context", border_style="dim")
        table.add_column("Category", style="cyan")
        table.add_column("Distance", style="yellow", justify="right")
        table.add_column("Snippet", style="dim")
        for m in matches:
            table.add_row(m["category"], f"{m['distance']:.4f}", m["content"][:120] + "...")
        console.print(table)

    run_sync(_ask())


@app.command(name="apply")
def apply_cmd(
    db_path: str = typer.Option(
        "data/open_job_loop.duckdb",
        "--db",
        help="Path to the DuckDB database file.",
    ),
    headless: bool = typer.Option(
        True,
        "--headless/--no-headless",
        help="Run browser in headless mode.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run/--no-dry-run",
        help="Simulate form filling without submitting.",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        "-n",
        help="Maximum number of shortlisted jobs to process.",
    ),
    resume_path: Optional[str] = typer.Option(
        "data/resume.pdf",
        "--resume-path",
        "-r",
        help="Path to candidate base resume PDF for automatic upload.",
    ),
    mock: bool = typer.Option(
        False,
        "--mock/--no-mock",
        help="Use mock embeddings and heuristics.",
    ),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        help="Enable interactive terminal prompts when new questions appear (default is 100% autonomous).",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="Ollama LLM model tag for form answering and active learning (default: qwen2.5-coder:3b).",
    ),
    deep: bool = typer.Option(
        False,
        "--deep",
        help="Use high-precision model (qwen2.5-coder:7b) for ATS form synthesis.",
    ),
) -> None:
    """
    Execute LinkedIn Easy Apply & ATS (Greenhouse, Lever, Ashby) automation for all SHORTLISTED jobs.
    """
    effective_ats_model = DEEP_HIGH_PRECISION_MODEL if deep else (model or DEFAULT_DEEP_MODEL)

    async def _apply() -> None:
        worker = AutoApplyWorker(
            db_path=db_path,
            headless=headless,
            dry_run=dry_run,
            mock_mode=mock,
            resume_path=resume_path,
            interactive_active_learning=interactive,
            llm_model=effective_ats_model,
        )
        stats = await worker.run(limit=limit)
        console.print(
            Panel(
                f"[bold green]Auto-Apply Execution Finished[/]\n"
                f"Processed: [bold]{stats['processed']}[/] | "
                f"Applied: [bold green]{stats['applied']}[/] | "
                f"Failed: [bold red]{stats['failed']}[/] | "
                f"Skipped: [bold yellow]{stats['skipped']}[/]",
                title="[bold cyan]Multi-Channel Application Summary[/]",
                border_style="green" if stats["applied"] > 0 else "yellow",
            )
        )

    run_sync(_apply())


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
    easy_apply: bool = typer.Option(
        True,
        "--easy-apply/--no-easy-apply",
        help="Filter search strictly to LinkedIn Easy Apply positions (default: True).",
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
        "data/open_job_loop.duckdb",
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
    model_fast: str = typer.Option(
        DEFAULT_FAST_MODEL,
        "--model-fast",
        help="Ultra-fast gatekeeper model tag for Tier 1 triage (default: llama3.2:1b).",
    ),
    model_deep: str = typer.Option(
        DEFAULT_DEEP_MODEL,
        "--model-deep",
        help="Deep technical evaluation model tag for Tier 2 (default: qwen2.5-coder:3b).",
    ),
    deep: bool = typer.Option(
        False,
        "--deep",
        help="Switch Tier 2 model to qwen2.5-coder:7b for maximum technical precision.",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="Legacy override to enforce a single model tag across triage evaluation.",
    ),
    no_cascade: bool = typer.Option(
        False,
        "--no-cascade",
        help="Disable 3-tier cascade and run direct single-stage evaluation.",
    ),
    base_url: str = typer.Option(
        os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
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
    apply: bool = typer.Option(
        False,
        "--apply/--no-apply",
        help="Automatically trigger LinkedIn Easy Apply for SHORTLISTED jobs.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run/--no-dry-run",
        help="When --apply is active, simulate form filling without submitting.",
    ),
    resume_path: Optional[str] = typer.Option(
        "data/resume.pdf",
        "--resume-path",
        "-r",
        help="Path to candidate base resume PDF for automatic upload.",
    ),
) -> None:
    """
    Execute the autonomous closed-loop pipeline (Discover -> Dedup -> Truncate -> Triage -> Decision Tree).
    """
    effective_deep = DEEP_HIGH_PRECISION_MODEL if deep else model_deep
    if model:
        effective_deep = model
        effective_fast = model
    else:
        effective_fast = model_fast

    display_model = f"Cascade ({effective_fast} -> {effective_deep})" if not no_cascade else effective_deep

    if not headless and console.is_terminal:
        render_banner(console=console, model=display_model, db_path=db_path)

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
            model=display_model,
            db_path=db_path,
            max_iterations=max_iterations,
        )

        with ui:
            ui.log_event("Initializing DuckDB repository and local LLM evaluator...")

            repo = JobRepository(db_path=db_path)
            await repo.initialize()

            if no_cascade:
                evaluator = JobFitEvaluator(
                    base_url=base_url,
                    model=effective_deep,
                    timeout=timeout,
                    score_threshold=threshold,
                )
            else:
                evaluator = CascadeJobEvaluator(
                    base_url=base_url,
                    model_fast=effective_fast,
                    model_deep=effective_deep,
                    timeout=timeout,
                    score_threshold=threshold,
                    db_path=db_path,
                    mock_mode=mock,
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
                mcp_client = McpJobClient(
                    command="uvx",
                    args=["mcp-server-linkedin@latest"],
                    tool_name="search_jobs",
                    default_tool_args={
                        "keywords": keywords,
                        "location": location,
                        "easy_apply": easy_apply,
                    },
                )

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

            if apply and metrics.get("shortlisted", 0) > 0:
                ui.log_event("Triggering Auto-Apply worker for SHORTLISTED jobs...")
                worker = AutoApplyWorker(
                    db_path=db_path,
                    headless=headless,
                    dry_run=dry_run,
                    mock_mode=mock,
                    resume_path=resume_path,
                )
                apply_stats = await worker.run(limit=limit)
                console.print(
                    Panel(
                        f"[bold green]Auto-Apply Finished:[/] Applied: [bold]{apply_stats['applied']}[/], "
                        f"Failed: [bold]{apply_stats['failed']}[/], Skipped: [bold]{apply_stats['skipped']}[/]",
                        title="[bold cyan]LinkedIn Application Step[/]",
                        border_style="green",
                    )
                )

        return 0

    exit_code = run_sync(_execute_loop())
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command(name="audit")
def audit_cmd(
    db_path: str = typer.Option(
        "data/open_job_loop.duckdb",
        "--db",
        help="Path to DuckDB database file.",
    ),
    limit: int = typer.Option(
        10,
        "--limit",
        "-n",
        help="Number of audit records to show.",
    ),
) -> None:
    """
    Display audit log of submitted job applications and filled form fields from DuckDB.
    """
    import duckdb
    import json
    import shutil
    import tempfile

    if not Path(db_path).exists():
        console.print(f"[yellow]Banco de dados '{db_path}' não encontrado.[/yellow]")
        return

    tmp_snapshot = None
    try:
        try:
            conn = duckdb.connect(db_path, read_only=True)
        except duckdb.IOException:
            with tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False) as tmp:
                tmp_snapshot = tmp.name
            shutil.copyfile(db_path, tmp_snapshot)
            conn = duckdb.connect(tmp_snapshot, read_only=True)

        try:
            conn.execute("INSTALL vss; LOAD vss;")
        except Exception:
            pass

        exists = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'application_audit_log'"
        ).fetchone()[0]
        if not exists:
            console.print("[yellow]Tabela application_audit_log ainda não possui registros.[/yellow]")
            return

        rows = conn.execute(
            """
            SELECT applied_at, job_title, company, channel, status, fields_submitted, logs
            FROM application_audit_log
            ORDER BY applied_at DESC
            LIMIT ?;
            """,
            [limit],
        ).fetchall()

        if not rows:
            console.print("[yellow]Nenhuma candidatura registrada na auditoria até o momento.[/yellow]")
            return

        table = Table(title="📜 Application Audit Log (DuckDB)", border_style="cyan")
        table.add_column("Applied At", style="dim", width=19)
        table.add_column("Company", style="bold white", width=18)
        table.add_column("Job Title", style="cyan", width=28)
        table.add_column("Channel", style="magenta", width=16)
        table.add_column("Status", style="green", width=14)
        table.add_column("Fields / Summary", style="white")

        for r in rows:
            applied_at, title, comp, chan, st, fields_raw, lgs = r
            dt_str = str(applied_at)[:19] if applied_at else ""
            summary_parts = []
            try:
                fields_list = json.loads(fields_raw) if isinstance(fields_raw, str) else (fields_raw or [])
                for f in fields_list:
                    if isinstance(f, dict) and "question" in f and "answer" in f:
                        summary_parts.append(f"{f['question'][:25]}: [green]{f['answer'][:20]}[/]")
            except Exception:
                pass
            fields_str = " | ".join(summary_parts[:3]) or (lgs or "")[:60]
            st_color = "green" if "APPLIED" in st else "red"
            table.add_row(dt_str, (comp or "")[:18], (title or "")[:28], chan or "", f"[{st_color}]{st}[/]", fields_str)

        console.print(table)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if tmp_snapshot and os.path.exists(tmp_snapshot):
            try:
                os.unlink(tmp_snapshot)
            except Exception:
                pass


@app.command(name="logs")
def logs_cmd(
    db_path: str = typer.Option(
        "data/open_job_loop.duckdb",
        "--db",
        help="Path to the DuckDB database file.",
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        "-n",
        help="Number of log records to show.",
    ),
    level: Optional[str] = typer.Option(
        None,
        "--level",
        "-l",
        help="Filter logs by level (e.g. INFO, WARNING, ERROR).",
    ),
    module: Optional[str] = typer.Option(
        None,
        "--module",
        "-m",
        help="Filter logs by module name substring.",
    ),
) -> None:
    """
    Display system and worker execution logs from DuckDB execution_logs table.
    """
    import duckdb
    import shutil
    import tempfile

    if not Path(db_path).exists():
        console.print(f"[yellow]Banco de dados '{db_path}' não encontrado.[/yellow]")
        return

    tmp_snapshot = None
    try:
        try:
            conn = duckdb.connect(db_path, read_only=True)
        except duckdb.IOException:
            with tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False) as tmp:
                tmp_snapshot = tmp.name
            shutil.copyfile(db_path, tmp_snapshot)
            conn = duckdb.connect(tmp_snapshot, read_only=True)

        try:
            conn.execute("INSTALL vss; LOAD vss;")
        except Exception:
            pass

        exists = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'execution_logs'"
        ).fetchone()[0]
        if not exists:
            console.print("[yellow]Tabela execution_logs ainda não possui registros.[/yellow]")
            return

        query = "SELECT timestamp, level, module, message FROM execution_logs WHERE 1=1"
        params: list[Any] = []
        if level:
            query += " AND UPPER(level) = ?"
            params.append(level.upper())
        if module:
            query += " AND module LIKE ?"
            params.append(f"%{module}%")
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()

        if not rows:
            console.print("[yellow]Nenhum registro encontrado em execution_logs.[/yellow]")
            return

        table = Table(title="🖥️  System Execution Logs (DuckDB)", border_style="blue")
        table.add_column("Timestamp", style="dim", width=19)
        table.add_column("Level", width=9)
        table.add_column("Module", style="cyan", width=25)
        table.add_column("Message", style="white")

        for r in rows:
            ts, lvl, mod, msg = r
            ts_str = str(ts)[:19] if ts else ""
            lvl_upper = str(lvl).upper()
            if lvl_upper == "ERROR":
                lvl_str = "[bold red]ERROR[/]"
            elif lvl_upper == "WARNING":
                lvl_str = "[bold yellow]WARN[/]"
            else:
                lvl_str = "[green]INFO[/]"

            table.add_row(ts_str, lvl_str, str(mod)[:25], str(msg))

        console.print(table)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if tmp_snapshot and os.path.exists(tmp_snapshot):
            try:
                os.unlink(tmp_snapshot)
            except Exception:
                pass


# ==============================================================================
# Script Entry Point
# ==============================================================================

def main() -> None:
    """Console script entrypoint for open-job-loop and jobloop."""
    app()


if __name__ == "__main__":
    main()
