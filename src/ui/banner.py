"""
Startup ASCII art banner and Rich Panel rendering for open-job-loop.

Provides high-contrast ASCII art, tagline, system configuration metadata,
and graceful plain-text fallback for non-TTY terminals.
"""

from __future__ import annotations

from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

# ==============================================================================
# Banner Constants
# ==============================================================================

ASCII_ART = r"""
  ___  ____  _____ _   _       _  ___  ____    _     ___   ___  ____  
 / _ \|  _ \| ____| \ | |     | |/ _ \| __ )  | |   / _ \ / _ \|  _ \ 
| | | | |_) |  _| |  \| |  _  | | | | |  _ \  | |  | | | | | | | |_) |
| |_| |  __/| |___| |\  | | |_| | |_| | |_) | | |__| |_| | |_| |  __/ 
 \___/|_|   |_____|_| \_|  \___/ \___/|____/  |_____\___/ \___/|_|    
""".strip("\n")

TAGLINE = "Privacy-First Local Job Search & Triage Agent"
DEFAULT_VERSION = "0.1.0"
DEFAULT_MODEL = "llama3.2:3b"
DEFAULT_DB = "open_job_loop.duckdb"


# ==============================================================================
# Banner Rendering Functions
# ==============================================================================

def get_banner_text(version: str = DEFAULT_VERSION) -> str:
    """
    Return the raw unformatted ASCII art banner and tagline as a string.
    Useful for plain-text output, logs, or headless environments.
    """
    header = f"OPEN-JOB-LOOP v{version}"
    return f"{header}\n{ASCII_ART}\n{TAGLINE}\n"


def get_banner_panel(
    version: str = DEFAULT_VERSION,
    model: str = DEFAULT_MODEL,
    db_path: str = DEFAULT_DB,
    border_style: str = "cyan",
) -> Panel:
    """
    Construct a formatted Rich Panel containing the ASCII art banner,
    subtitles, and local runtime configuration metadata.

    Args:
        version: Package version string.
        model: Target local LLM model name.
        db_path: Path to DuckDB database file.
        border_style: Color style for panel borders.

    Returns:
        Rich Panel instance ready for console rendering.
    """
    art_text = Align.center(Text(ASCII_ART, style="bold cyan", no_wrap=True))

    meta_line1 = Text.from_markup(
        f" [cyan]• LLM Engine:[/] [bright_white]{model} (Local Ollama)[/]   "
        f" [cyan]• Persistence:[/] [bright_white]DuckDB ({db_path})[/]"
    )
    meta_line2 = Text.from_markup(
        " [cyan]• Ingestion:[/] [bright_white]MCP / Mock[/]               "
        " [cyan]• Harness:[/] [bright_white]LocalLoopGuard (Timeout & Circuit)[/]"
    )

    body = Group(
        art_text,
        Text(""),
        Align.center(meta_line1),
        Align.center(meta_line2),
    )

    return Panel(
        body,
        title=f"[bold bright_white] OPEN-JOB-LOOP v{version} [/]",
        subtitle=f"[bold yellow] {TAGLINE} [/]",
        border_style=border_style,
        padding=(1, 1),
    )


def render_banner(
    console: Console | None = None,
    version: str = DEFAULT_VERSION,
    model: str = DEFAULT_MODEL,
    db_path: str = DEFAULT_DB,
    plain: bool = False,
) -> None:
    """
    Render the startup banner to the console.

    Args:
        console: Optional Rich Console instance. If None, a standard console is used.
        version: Application version string.
        model: Model name.
        db_path: DuckDB database path.
        plain: If True, renders plain text without Rich styling or borders.
    """
    c = console or Console()

    if plain or not c.is_terminal:
        # Fallback to clean plain text for non-interactive shells or plain requests
        c.print(get_banner_text(version=version))
        return

    c.print(
        get_banner_panel(
            version=version,
            model=model,
            db_path=db_path,
        )
    )
