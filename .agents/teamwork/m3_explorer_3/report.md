# M3 Engineering Design Report: Rich UI & Typer CLI Subsystem

**Agent**: M3 Explorer 3 (`teamwork_preview_explorer`)  
**Scope**: Requirements R1, R2, R4; Typer CLI (`src/cli.py`), Rich Banner (`src/ui/banner.py`), Rich Console UI (`src/ui/console.py`), and Test Suite (`tests/test_cli.py`).  
**Working Directory**: `/Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m3_explorer_3`  
**Date**: 2026-10-05  

---

## 1. Executive Summary

This report establishes the complete, production-ready code design, architectural interfaces, and test specifications for the user interface and command-line harness of **open-job-loop**. 

The implementation delivers:
1. **`src/ui/banner.py`**: Predefined 80-column ASCII art banner (`OPEN-JOB-LOOP`), tagline (`Privacy-First Local Job Search & Triage Agent`), and Rich `Panel` presentation embedding local LLM engine specs, MCP ingestion details, and DuckDB persistence guarantees.
2. **`src/ui/console.py`**: Rich live layout (`rich.live.Live`) with synchronized 5-stage DAG pipeline status indicators (`[✓]`, `[⟳]`, `[•]`, `[⏱]`), candidate job card detailing match fit scores and skill tags, metrics counters, and automated fallback to `HeadlessPipelineUI` for non-TTY terminals, pipes, and CI.
3. **`src/cli.py`**: Typer application exposing subcommands `banner`, `stats`, and `run`, options for keyword search, limits, fit score thresholds, DuckDB persistence paths, timeout bounding, mock fixture replay, and dual script entrypoints (`jobloop` and `open-job-loop`).
4. **`tests/test_cli.py`**: Complete unit and integration test suite (15 tests) testing ASCII rendering, stats calculation, option validation, headless execution, and custom fixture processing using `typer.testing.CliRunner`.

All components have been tested in Python 3.12 with 100% test pass rate.

---

## 2. Component Design & Specifications

### 2.1 `src/ui/banner.py` (ASCII Art & Rich Panel)

#### Architectural Purpose:
Provides visual identity, satisfies acceptance criteria ("The CLI starts up and renders the predefined ASCII art banner"), and displays runtime environment specs.

#### Predefined ASCII Art:
```
  ___  ____  _____ _   _       _  ___  ____    _     ___   ___  ____  
 / _ \|  _ \| ____| \ | |     | |/ _ \| __ )  | |   / _ \ / _ \|  _ \ 
| | | | |_) |  _| |  \| |  _  | | | | |  _ \  | |  | | | | | | | |_) |
| |_| |  __/| |___| |\  | | |_| | |_| | |_) | | |__| |_| | |_| |  __/ 
 \___/|_|   |_____|_| \_|  \___/ \___/|____/  |_____\___/ \___/|_|    
```
- **Geometry**: 70 columns wide, 5 lines tall. Fits standard 80-column terminals with borders and padding.
- **Tagline**: `"Privacy-First Local Job Search & Triage Agent"`

#### API Specification:
- `get_banner_text(version: str = "0.1.0") -> str`: Returns unstyled ASCII art for plain text logging.
- `get_banner_panel(version: str = "0.1.0", model: str = "llama3.2:3b", db_path: str = "open_job_loop.duckdb", border_style: str = "cyan") -> Panel`: Returns centered Rich `Panel` containing metadata bullets.
- `render_banner(console: Optional[Console] = None, version: str = "0.1.0", model: str = "llama3.2:3b", db_path: str = "open_job_loop.duckdb", plain: bool = False) -> None`: High-level print function with automated non-TTY detection.

#### Artifact Reference:
Full source code written to:
`/Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m3_explorer_3/proposed_banner.py`

---

### 2.2 `src/ui/console.py` (Live Layout & Telemetry Dashboard)

#### Architectural Purpose:
Provides real-time terminal telemetry during closed-loop execution. Dispatches DAG stage transitions, displays active candidate evaluation results, and maintains counters without terminal flickering.

#### Layout Architecture:
A 3-tier `rich.layout.Layout`:
- **Header (`Layout(name="header", size=4)`)**: Displays search parameters, candidate profile, target model, DuckDB path, and elapsed wall-clock timer.
- **Body (`Layout(name="body", size=11)`)**: Split horizontally:
  - Left (`pipeline`, ratio 1): 5-stage DAG pipeline tracking (`STAGE_NAMES`: Ingestion, Deduplication, Pre-Processing, Triage, Decision Tree) with state indicators (`✓ Done`, `⟳ Running...`, `• Pending`, `⏱ Timeout`, `↷ Skipped`, `✗ Error`).
  - Right (`current_job`, ratio 2): Candidate job card showing title, hiring organization, location, current lifecycle status, evaluated fit score badge, matched skills list, missing skills list, and reasoning summary.
- **Footer (`Layout(name="footer", size=3)`)**: Live counters for Discovered, Ingested, Duplicates, Shortlisted, Discarded, Timeouts, and Errors.

#### Headless & Non-TTY Support:
Factory function `create_pipeline_ui(headless: bool = False, console: Optional[Console] = None, **kwargs)` inspects `--headless` and `console.is_terminal`:
- If interactive TTY: Instantiates `LivePipelineUI` using `rich.live.Live(refresh_per_second=4)`.
- If non-TTY, CI, or `--headless`: Instantiates `HeadlessPipelineUI`, emitting timestamped, single-line log messages (`[HEADLESS]`, `[STAGE]`, `[JOB]`, `[TRIAGE]`, `[SKIPPED]`) without cursor repositioning.

#### Telemetry Hooks Interface:
```python
class BasePipelineUI(abc.ABC):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def on_stage_update(self, stage_idx: int, stage_name: str, status: str, detail: Optional[str] = None) -> None: ...
    def on_job_start(self, job: JobPosting, iteration: int, max_iterations: int) -> None: ...
    def on_job_triaged(self, job: JobPosting, evaluation: MatchEvaluation) -> None: ...
    def on_job_completed(self, job: JobPosting, metrics: Dict[str, int]) -> None: ...
    def on_job_skipped(self, reason: str, job: Optional[JobPosting] = None) -> None: ...
    def on_metrics_update(self, metrics: Dict[str, int]) -> None: ...
    def log_event(self, message: str, level: str = "info") -> None: ...
    def print_summary(self, metrics: Dict[str, int], duration_seconds: float) -> None: ...
```

#### Artifact Reference:
Full source code written to:
`/Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m3_explorer_3/proposed_console.py`

---

### 2.3 `src/cli.py` (Typer CLI Application)

#### Architectural Purpose:
Serves as the user-facing command-line entry point, defining commands, validating input arguments, coordinating subsystem lifecycles, and managing error exits.

#### Commands & Options:

1. **`jobloop banner`**:
   - `--plain`: Emit unstyled text.
   - `--version`: Specify version string.
2. **`jobloop stats`**:
   - `--db`: DuckDB path (default: `open_job_loop.duckdb`).
   - Connects to `JobRepository`, aggregates status counts via `get_stats()`, and displays a formatted Rich Table with categories, counts, shares, and overall Shortlist Fit Rate. If empty, displays an informative prompt.
3. **`jobloop run`**:
   - `--keywords` / `-k` (default: `"Python Software Engineer"`)
   - `--location` / `-l` (default: `"Remote"`)
   - `--limit` / `-n` (default: `10`, `min=1`)
   - `--threshold` / `-t` (default: `70`, `min=0, max=100`)
   - `--mock / --no-mock` (default: `False`)
   - `--fixture` (default: `None`)
   - `--db` (default: `"open_job_loop.duckdb"`)
   - `--timeout` (default: `30.0`, `min=0.1`)
   - `--max-iterations` (default: `50`, `min=1`)
   - `--model` (default: `"llama3.2:3b"`)
   - `--base-url` (default: `"http://localhost:11434/v1"`)
   - `--headless / --no-headless` (default: `False`)
   - `--candidate-name` / `--candidate-summary`

#### Nested Event Loop Defense (`run_sync`):
Typer commands execute synchronously from Click's callback system. In automated test environments running under `pytest-asyncio`, an async event loop is already bound to the active thread. `src/cli.py` implements `run_sync(coro)`:
- Detects whether an event loop is active and running via `asyncio.get_running_loop()`.
- If no loop is running, executes `asyncio.run(coro)`.
- If a loop is already running, submits execution to `concurrent.futures.ThreadPoolExecutor(max_workers=1)` to avoid `RuntimeError: asyncio.run() cannot be called from a running event loop`.

#### Console Script Packaging Compatibility:
`pyproject.toml` binds:
```toml
[project.scripts]
open-job-loop = "src.cli:app"
jobloop = "src.cli:app"
```
Because Typer instances are callables, `src.cli:app` is directly executable by Python script wrappers. A standard `main()` function is also provided for `python -m src.cli`.

#### Artifact Reference:
Full source code written to:
`/Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m3_explorer_3/proposed_cli.py`

---

### 2.4 `tests/test_cli.py` (Test Suite)

#### Architectural Purpose:
Verifies CLI command parsing, exit codes, option validations, Rich UI rendering, and mock pipeline execution under `typer.testing.CliRunner`.

#### Test Inventory:
| # | Test Function | Target Verified | Method |
|---|---------------|-----------------|--------|
| 1 | `test_cli_app_callable` | Entry points | Asserts `app` and `main` are callable |
| 2 | `test_cli_help` | Help output | Tests `open-job-loop --help`, verifies commands |
| 3 | `test_cli_banner_command` | ASCII banner | Verifies `jobloop banner`, checks art & tagline |
| 4 | `test_cli_banner_plain_option` | Plain banner | Verifies `jobloop banner --plain` |
| 5 | `test_banner_text_helper` | Banner helper | Direct string inspection |
| 6 | `test_banner_panel_helper` | Panel builder | Rich Panel title & metadata validation |
| 7 | `test_cli_stats_empty_db` | Stats command | Empty DB notice rendering |
| 8 | `test_cli_stats_populated_db` | Stats command | Populated DuckDB table, counts, fit rate |
| 9 | `test_cli_run_invalid_limit` | Validation | `--limit 0` rejected with exit code 2 |
| 10 | `test_cli_run_invalid_threshold` | Validation | `--threshold 150` rejected with exit code 2 |
| 11 | `test_cli_run_mock_headless` | End-to-end loop | Autonomous 5-stage loop execution, DB persistence |
| 12 | `test_cli_run_custom_fixture` | Fixture replay | Custom JSON fixture loaded and triaged |
| 13 | `test_ui_factory_selection` | UI Factory | Verification of HeadlessPipelineUI selection |
| 14 | `test_headless_ui_lifecycle` | Headless UI | Logging lifecycle without errors |
| 15 | `test_live_ui_layout_generation` | Live UI layout | Verification of layout children and renderables via `Console.capture()` |

#### Artifact Reference:
Full source code written to:
`/Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m3_explorer_3/proposed_test_cli.py`

---

## 3. Integration Blueprint

### Interface Contract with M3 Explorer 1 (`LocalLoopGuard` & `MCPCircuitBreaker`):
- `LocalLoopGuard` from `src.core.harness` guards inference timeouts and bounded iterations.
- If inference exceeds `--timeout`, `LocalLoopGuard` catches `TimeoutError`, marks `JobStatus.SKIPPED_TIMEOUT`, logs warning, and commits state to DuckDB.
- `src/cli.py` passes `max_iterations`, `timeout_seconds`, and `repository` to `LocalLoopGuard`.

### Interface Contract with M3 Explorer 2 (`JobPipeline`):
- `JobPipeline` from `src.core.pipeline` consumes `ui_listener=ui` (implementing `BasePipelineUI`).
- Stages emit `on_stage_update(stage_idx, stage_name, status, detail)` to reflect real-time progress.
- `src/cli.py` instantiates `JobPipeline` if available, or executes the self-contained direct DAG loop as fallback.

---

## 4. Verification & Readiness Assessment

1. **All 15 proposed tests pass** deterministically in `.venv` (Python 3.12.13).
2. **Terminal ergonomics verified**: Tested across standard 80-column displays and headless redirects.
3. **DuckDB immediate flush verified**: Data written and read back via `stats` command.
4. **No circular imports**: Clean separation of `src.ui.banner`, `src.ui.console`, and `src.cli`.
