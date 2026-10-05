# Technical Survey Report: MCP Ingestion, Typer CLI, Rich UI & Architecture

**Agent**: Survey Agent 2 (`explorer_survey_2`)  
**Scope**: Requirements R1, R2, R4, Reference Architectures, MCP Ingestion, Typer/Rich UI, Packaging  
**Target File**: `/Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/explorer_survey_2/survey_mcp_ui.md`  
**Date**: 2026-10-05  

---

## 1. Executive Summary

This survey provides comprehensive technical specifications, architectural patterns, and concrete code blueprints for implementing **open-job-loop**: an autonomous, local-first CLI agent that discovers, deduplicates, triages, and structures job postings using Model Context Protocol (MCP) servers, local Llama 3.2 inference, and a terminal UI built with Typer and Rich.

### Verified Local Environment
- **Host OS**: macOS (Darwin 24.x, Apple Silicon)
- **Python**: Python 3.12.13 located at `/opt/homebrew/bin/python3.12` (satisfies Python 3.12+ requirement).
- **Local LLM Engine**: Ollama running locally at `http://localhost:11434` with `llama3.2:3b` (parameter size 3.2B, context length 131,072) and `llama3.2:1b` installed and confirmed operational via OpenAI-compatible endpoint `/v1/models`.
- **Packaging Tools**: Standard `python3.12 -m venv` and `pip` available; PEP 621 `pyproject.toml` using `hatchling` provides direct compatibility with both `pip` and Astral `uv`.

---

## 2. Requirement R1: Local-First Execution & Architecture

### 2.1 Async Python 3.12+ Architecture
The core runtime must be fully asynchronous using Python 3.12's `asyncio` features (e.g., `asyncio.TaskGroup`, structured concurrency, asynchronous context managers). All network I/O (MCP stdio/SSE communication, Ollama HTTP calls) must be non-blocking.

### 2.2 Local OpenAI SDK & Instructor Integration
All LLM inferences use the official `openai.AsyncOpenAI` SDK, pointing to the local endpoint:
```python
from openai import AsyncOpenAI
import instructor

client = AsyncOpenAI(
    base_url="http://localhost:11434/v1",
    api_key="ollama",  # Placeholder required by OpenAI client
)

# Instructor patched client with JSON mode for local Llama 3.2 models
aclient = instructor.from_openai(client, mode=instructor.Mode.JSON)
```

**Key Discovery for Llama 3.2 via Ollama**:
- `instructor.Mode.JSON` is the most reliable mode for local small open-weight models (`llama3.2:3b` and `llama3.2:1b`), because tool-calling formats can vary across local backends, whereas structured JSON output with Pydantic JSON schema injection in the system prompt guarantees deterministic adherence.
- Validation retries: `max_retries=2` inside `aclient.chat.completions.create(...)` allows Instructor to feed parsing errors back to the model if validation fails.

### 2.3 TextTruncator Design & Token Budget Limiting
Local models running under Ollama typically have default context limits configured in memory (`num_ctx`, often 2048 to 8192 tokens by default, though the model architecture supports up to 128k). Real-world job postings from LinkedIn often exceed 3,000 words due to boilerplate text (EEO statements, company legal disclaimers, generic benefits descriptions).

`TextTruncator` must enforce a safe token threshold (default: 2,000 tokens ~ 8,000 characters):
1. **Token Estimation**: Fast counting using `tiktoken` (encoding `cl100k_base` as proxy for BPE tokenization) with fallback heuristic `token_count = len(text) // 4`.
2. **Smart Boilerplate Pruning**: Strip trailing legal boilerplate (e.g., "Equal Opportunity Employer", "affirmative action", "privacy policy").
3. **Truncation Strategy**:
   - Retain header (Title, Company, Location, Summary) — top 25%.
   - Retain requirements, qualifications, and core responsibilities — middle 60%.
   - Truncate middle or trailing non-essential sections if token limit exceeded, appending `[...truncated...]`.

### 2.4 Immediate Persistence: DuckDB / SQLModel
Requirement R1 mandates: *“Stateful operations must be flushed to a local DuckDB / SQLModel database immediately; no large arrays kept in RAM.”*

#### Recommended Persistence Architecture:
- Database engine: DuckDB file-backed database (`data/jobs.duckdb`).
- ORM / Mapping: `SQLModel` with `duckdb-engine` (`duckdb:///data/jobs.duckdb`), or native DuckDB connection with Pydantic serialization.
- **Architectural Decision**: `SQLModel` provides dual utility — inheriting from Pydantic `BaseModel` for validation, serializing directly to JSON, while defining database tables. 
- **Immediate Flush Contract**:
  - Ingestion stage: Ingest single posting -> compute SHA256 -> check DuckDB existence -> if unique, insert with status `INGESTED` and commit immediately.
  - Triage stage: Fetch single `INGESTED` posting -> execute LLM evaluation -> update record with `fit_score`, `fit_reasoning`, and `JobStatus.SHORTLISTED` / `DISCARDED` -> commit immediately.
  - No accumulating lists or queues of full job descriptions in memory. Processing operates as an async generator (`async for job in pipeline`).

---

## 3. Requirement R2: Data Flow, State Machine & DAG Pipeline

### 3.1 Strict DAG Pipeline Flow
The data flow follows a strict 5-stage Directed Acyclic Graph (DAG):

```
┌─────────────────┐
│ 1. Ingestion    │ (via MCP tool: search_jobs / get_job_details / Mock)
└────────┬────────┘
         ▼
┌─────────────────┐
│ 2. Deduplication│ (SHA256 hash in DuckDB; duplicate -> exit early)
└────────┬────────┘
         ▼
┌─────────────────┐
│ 3. Pre-process  │ (TextTruncator token limiting & boilerplate strip)
└────────┬────────┘
         ▼
┌─────────────────┐
│ 4. Triage       │ (Llama 3.2 via instructor -> MatchEvaluation)
└────────┬────────┘
         ▼
┌─────────────────┐
│ 5. Decision Tree│ (fit_score >= threshold -> SHORTLISTED else DISCARDED)
└─────────────────┘
```

### 3.2 Pydantic Data Models & Schemas

```python
from datetime import datetime
from enum import Enum
from typing import Optional, Literal
from sqlmodel import SQLModel, Field
from pydantic import BaseModel, Field as PyField


class JobStatus(str, Enum):
    DISCOVERED = "discovered"
    INGESTED = "ingested"
    DUPLICATE = "duplicate"
    PREPROCESSED = "preprocessed"
    SHORTLISTED = "shortlisted"
    DISCARDED = "discarded"
    FAILED = "failed"


class MatchEvaluation(BaseModel):
    """Deterministic structured output generated by local Llama 3.2 via Instructor."""
    fit_score: float = PyField(
        ..., 
        ge=0.0, 
        le=1.0, 
        description="Candidate technical fit score from 0.0 (no fit) to 1.0 (perfect fit)"
    )
    reasoning: str = PyField(
        ..., 
        description="Concise justification highlighting why the role matches or does not match"
    )
    matched_skills: list[str] = PyField(
        default_factory=list, 
        description="Key skills and requirements from the job description matched"
    )
    missing_skills: list[str] = PyField(
        default_factory=list, 
        description="Required skills or qualifications missing or weakly matched"
    )
    decision: Literal["shortlist", "discard"] = PyField(
        ..., 
        description="Triage decision: 'shortlist' if fit_score >= threshold else 'discard'"
    )


class JobPosting(SQLModel, table=True):
    """DuckDB / SQLModel table storing job posting lifecycle."""
    __tablename__ = "job_postings"

    id: str = Field(primary_key=True)
    title: str = Field(index=True)
    company: str = Field(index=True)
    location: Optional[str] = Field(default=None)
    url: Optional[str] = Field(default=None)
    source: str = Field(default="mcp")
    sha256_hash: str = Field(index=True, unique=True)
    
    raw_description: str
    cleaned_description: Optional[str] = Field(default=None)
    token_count: Optional[int] = Field(default=None)
    
    status: JobStatus = Field(default=JobStatus.INGESTED, index=True)
    fit_score: Optional[float] = Field(default=None)
    fit_reasoning: Optional[str] = Field(default=None)
    matched_skills: Optional[str] = Field(default=None)  # JSON-serialized list
    missing_skills: Optional[str] = Field(default=None)  # JSON-serialized list
    
    error_message: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
```

### 3.3 Deduplication Strategy
1. **Hash Key**: Compute SHA-256 over normalized canonical fields:
   `key = f"{title.lower().strip()}|{company.lower().strip()}|{raw_description.strip()}"`
   `sha256_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()`
2. **DuckDB Lookup**:
   Execute `SELECT id FROM job_postings WHERE sha256_hash = :hash`.
   If match found: mark posting status as `JobStatus.DUPLICATE`, log event, and bypass LLM triage immediately.

---

## 4. MCP Ingestion Integration & Patterns

### 4.1 MCP (Model Context Protocol) Overview
MCP defines a standardized protocol over JSON-RPC 2.0 allowing AI clients to discover and invoke tools provided by servers. In Python, the official SDK is `mcp` (`from mcp import ClientSession, StdioServerParameters`).

### 4.2 Reference Analysis: `stickerdaniel/linkedin-mcp-server`
The reference repository provides a real-world MCP server interface to LinkedIn:
- **Transport**: Standard I/O (`stdio`) or Streamable HTTP / SSE.
- **Tools**:
  - `search_jobs(keywords: str, location: str, ...)`: Returns a list of job search results with metadata and IDs.
  - `get_job_details(job_id: str)`: Returns full job description, company details, and requirements.
  - `get_job_apply_url(job_id: str)`: Returns direct application URL.
- **Operating Realities**:
  - Uses browser automation (Patchright/Playwright) on local session cookies.
  - Prone to login popups, session expiry, CAPTCHAs, and high latency (3-15 seconds per call).
  - Calling live LinkedIn during automated CI or unit tests risks account blocks and causes test non-determinism.

### 4.3 Reference Analysis: `career-ops` & `ai-job-search`
- **Career-ops Pattern**: Decoupled ingestion layer where job data can originate from local mock scrapers, saved fixtures, or live MCP endpoints. Scores jobs against a structured user profile CV (1-5 or 0.0-1.0 scale).
- **Claudex-loop Pattern**: Closed-loop harness with strict phase separation and validation guards before committing state.

### 4.4 Autonomous Ingestion Architecture: `McpClientAdapter`
To provide production reliability, offline testability, and live MCP support, the ingestion system implements the **Adapter Pattern**:

```python
from abc import ABC, abstractmethod
from typing import AsyncGenerator
from open_job_loop.models import RawJob

class BaseJobIngestionClient(ABC):
    @abstractmethod
    async def connect(self) -> None: ...
    
    @abstractmethod
    async def disconnect(self) -> None: ...
    
    @abstractmethod
    async def search_jobs(
        self, keywords: str, location: str, limit: int = 10
    ) -> AsyncGenerator[RawJob, None]: ...
```

#### Implementations:
1. **`LiveMcpClient`**:
   - Connects to `mcp-server-linkedin` or any MCP server using `mcp.client.stdio.stdio_client`.
   - Spawns subprocess using `StdioServerParameters(command="uvx", args=["mcp-server-linkedin@latest"])` or custom command.
   - Handshakes via `await session.initialize()`.
   - Invokes `await session.call_tool("search_jobs", arguments={"keywords": keywords, "location": location})`.
   - Parses the JSON payload from `result.content[0].text`.
2. **`MockMcpClient` / `FixtureMcpClient`**:
   - Implements the exact same MCP protocol handshake or in-process mock server.
   - Replays test job fixtures (e.g., `fixtures/golden_jobs.json`) with deterministic results.
   - Zero external network or browser dependencies for automated testing.

---

## 5. Requirement R4: Typer CLI & Rich UI

### 5.1 Typer Command Structure
The CLI is built with `typer` (v0.12+) exposing a modern command tree:

```
open-job-loop (root)
├── run          # Execute autonomous closed loop (MCP -> Dedup -> Triage -> Persist)
├── triage       # Triage pending ingested jobs without running ingestion
├── stats        # Display summary tables & metrics from DuckDB
├── inspect      # Display detailed Rich cards for specific job postings
├── export       # Export shortlisted candidates to JSON, CSV, or Markdown
└── banner       # Render the predefined ASCII art banner (acceptance test)
```

#### Typer Command Implementation Sketch:
```python
import typer
import asyncio
from typing import Optional

app = typer.Typer(
    name="open-job-loop",
    help="Autonomous, local-first job discovery and triage CLI agent.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

@app.command()
def run(
    keywords: str = typer.Option("Python Backend Engineer", "--keywords", "-k", help="Search keywords"),
    location: str = typer.Option("Remote", "--location", "-l", help="Job location"),
    limit: int = typer.Option(10, "--limit", "-n", help="Max jobs to process"),
    threshold: float = typer.Option(0.70, "--threshold", "-t", help="Fit score cutoff (0.0 - 1.0)"),
    mock: bool = typer.Option(False, "--mock", help="Use local mock MCP server fixtures"),
    db_path: str = typer.Option("data/jobs.duckdb", "--db", help="Path to DuckDB database"),
    timeout: float = typer.Option(30.0, "--timeout", help="Inference timeout per job in seconds"),
):
    """Run the autonomous ingestion, deduplication, and triage loop."""
    asyncio.run(execute_loop(keywords, location, limit, threshold, mock, db_path, timeout))

@app.command()
def banner():
    """Render startup ASCII art banner."""
    from open_job_loop.ui.banner import render_banner
    render_banner()
```

### 5.2 Rich UI Elements & Terminal Ergonomics

#### 1. Startup ASCII Art Banner
A high-contrast, professional ASCII art banner rendered inside a Rich `Panel` with cyan border and formatted system specs:

```
  ██████╗ ██████╗ ███████╗███╗   ██╗     ██╗ ██████╗ ██████╗     ██╗      ██████╗  ██████╗ ██████╗ 
 ██╔═══██╗██╔══██╗██╔════╝████╗  ██║     ██║██╔═══██╗██╔══██╗    ██║     ██╔═══██╗██╔═══██╗██╔══██╗
 ██║   ██║██████╔╝█████╗  ██╔██╗ ██║     ██║██║   ██║██████╔╝    ██║     ██║   ██║██║   ██║██████╔╝
 ██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║██   ██║██║   ██║██╔══██╗    ██║     ██║   ██║██║   ██║██╔═══╝ 
 ╚██████╔╝██║     ███████╗██║ ╚████║╚█████╔╝╚██████╔╝██████╔╝    ███████╗╚██████╔╝╚██████╔╝██║     
  ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝ ╚════╝  ╚═════╝ ╚═════╝     ╚══════╝ ╚═════╝  ╚═════╝ ╚═╝     
```

**Banner Specification**:
- Model: `llama3.2:3b` via Ollama (`http://localhost:11434/v1`)
- Persistence: DuckDB (`data/jobs.duckdb`)
- Ingestion: Model Context Protocol (MCP)
- Rendered with `rich.console.Console` and `rich.panel.Panel`.

#### 2. Live Updating Panels (`rich.live.Live`)
During execution of `jobloop run`, `rich.live.Live` provides real-time visual feedback without clearing scrollback unnecessarily:

```
┌─────────────────────────────────── OPEN JOB LOOP ───────────────────────────────────┐
│ Status: RUNNING  │ Model: llama3.2:3b │ Threshold: 0.70 │ Iteration: 3/10           │
└─────────────────────────────────────────────────────────────────────────────────────┘
┌── Pipeline Stage ───────────────┬── Current Candidate ──────────────────────────────┐
│ [✓] 1. Ingestion (MCP)          │ Title: Senior Python / AI Engineer                │
│ [✓] 2. Deduplication (DuckDB)   │ Company: TechCorp Labs                            │
│ [✓] 3. Text Truncator (1.8k tok)│ Fit Score: [bold green]0.88 / 1.00 (SHORTLIST)[/] │
│ [⟳] 4. Triage (Llama 3.2)       │ Matched: Python, FastAPI, DuckDB, Asyncio         │
│ [ ] 5. Decision & Persistence   │ Missing: Kubernetes                               │
└─────────────────────────────────┴───────────────────────────────────────────────────┘
┌── Live Metrics ─────────────────────────────────────────────────────────────────────┐
│ Discovered: 10 │ Ingested: 8 │ Duplicates: 2 │ Shortlisted: 5 │ Discarded: 3 │ Err: 0│
└─────────────────────────────────────────────────────────────────────────────────────┘
```

#### 3. Spinners and Status Indicators
- `rich.console.Console.status("[bold cyan]Connecting to MCP server...", spinner="dots")` for cold start operations.
- `rich.progress.Progress` with `SpinnerColumn`, `TextColumn`, `BarColumn`, `TaskProgressColumn`, `TimeRemainingColumn` for batch runs.

---

## 6. Project Packaging & Dependency Manifest

### 6.1 `pyproject.toml` (PEP 621 / Hatchling)

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "open-job-loop"
version = "0.1.0"
description = "Autonomous, privacy-first local CLI agent for job discovery, deduplication, and triage via MCP and Llama 3.2"
readme = "README.md"
requires-python = ">=3.12"
license = { text = "MIT" }
authors = [
    { name = "Mauricio Helfstein" }
]
dependencies = [
    "typer>=0.12.0",
    "rich>=13.7.0",
    "instructor>=1.4.0",
    "openai>=1.35.0",
    "mcp>=1.1.0",
    "sqlmodel>=0.0.19",
    "duckdb>=1.0.0",
    "duckdb-engine>=0.13.0",
    "pydantic>=2.7.0",
    "pydantic-settings>=2.2.0",
    "tiktoken>=0.7.0",
    "httpx>=0.27.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.2.0",
    "pytest-asyncio>=0.23.0",
]

[project.scripts]
open-job-loop = "open_job_loop.cli:app"
jobloop = "open_job_loop.cli:app"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

### 6.2 Tooling Compatibility
- **uv**: `uv venv --python 3.12`, `uv pip install -e .` or `uv run open-job-loop run`
- **pip / standard venv**: `/opt/homebrew/bin/python3.12 -m venv .venv && source .venv/bin/activate && pip install -e .`

---

## 7. Recommended Project File Structure

Following the modern `src/` layout:

```
open-job-loop/
├── pyproject.toml
├── README.md
├── fixtures/
│   └── golden_jobs.json          # 3 matches, 3 mismatches for acceptance testing
├── src/
│   └── open_job_loop/
│       ├── __init__.py
│       ├── cli.py                # Typer CLI app entry point & commands
│       ├── config.py             # Pydantic Settings (Ollama URL, model, DB path)
│       ├── core/
│       │   ├── __init__.py
│       │   ├── dag.py            # DAG Pipeline orchestrator
│       │   ├── truncator.py      # TextTruncator implementation
│       │   ├── triage.py         # Instructor + local AsyncOpenAI triage engine
│       │   └── harness.py        # LocalLoopGuard, timeout, circuit breaker (R3)
│       ├── db/
│       │   ├── __init__.py
│       │   ├── database.py       # DuckDB / SQLModel engine and session manager
│       │   └── repository.py     # Deduplication query & job state persistence
│       ├── mcp/
│       │   ├── __init__.py
│       │   ├── adapter.py        # MCP client interface & stdio transport
│       │   └── mock_server.py    # Local deterministic mock MCP server
│       ├── models/
│       │   ├── __init__.py
│       │   ├── schemas.py        # MatchEvaluation, JobStatus, RawJob
│       │   └── entities.py       # JobPosting SQLModel entity
│       └── ui/
│           ├── __init__.py
│           ├── banner.py         # ASCII art banner rendering
│           └── live_view.py      # Rich Live layout & panels
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_banner.py            # Tests banner rendering
    ├── test_truncator.py         # Tests token limits
    ├── test_mcp_adapter.py       # Tests MCP ingestion
    ├── test_dedup.py             # Tests SHA256 DuckDB dedup
    └── test_local_inference.py   # Golden tests against local Llama 3.2
```

---

## 8. Failure Modes, Edge Cases & Mitigations

| Failure Mode | Root Cause | Mitigation Strategy |
|---|---|---|
| **Ollama Inference Timeout** | Local model latency spike on CPU/GPU | `LocalLoopGuard` timeout guard catches `asyncio.TimeoutError` per job, marks status `FAILED`, and gracefully proceeds to next job. |
| **Context Window Exceeded** | 3000+ word job posting with legal terms | `TextTruncator` prunes boilerplates and truncates to 2000 tokens prior to prompting. |
| **Instructor JSON Parse Failure** | Small 3.2B model outputting malformed JSON | `mode=instructor.Mode.JSON` with `max_retries=2` automatically feeds error trace back to Ollama. |
| **LinkedIn MCP Browser Hang** | Bot detection, CAPTCHA, or expired cookie | MCP Circuit Breaker trips after 3 consecutive errors; fallback to mock/fixture or exit gracefully. |
| **DuckDB Lock Contention** | Multiple processes opening `.duckdb` file | Single-writer connection pool; immediate commit and close per transaction. |
| **Non-TTY Terminal Glitch** | Running in CI or redirected stdout (`| cat`) | Check `console.is_terminal`; disable `rich.live.Live` dynamic refresh, fall back to plain log lines. |

---

## 9. Conclusion & Recommendations for Orchestration

1. **Environment Ready**: Local Ollama has `llama3.2:3b` and `llama3.2:1b` already running on `http://localhost:11434/v1`.
2. **Packaging**: Use PEP 621 `pyproject.toml` with `hatchling` and dual console entry points `open-job-loop` and `jobloop`.
3. **MCP Ingestion**: Provide an abstract `BaseJobIngestionClient` with both `LiveMcpClient` and `MockMcpClient` (using `fixtures/golden_jobs.json`) so testing and offline evaluation do not depend on external browser sessions.
4. **UI**: Isolate banner rendering (`open_job_loop.ui.banner:render_banner`) so it can be verified both via unit test (`test_banner.py`) and CLI execution (`jobloop banner`).
