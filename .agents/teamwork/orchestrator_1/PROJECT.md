# Project: open-job-loop

## Architecture
Autonomous, privacy-first CLI agent executing closed loops to discover, deduplicate, evaluate technical fit, and structure job applications using local open-weight models (Llama 3.2 via Ollama) and Model Context Protocol (MCP).

```
                     ┌───────────────────────────────┐
                     │      Typer CLI + Rich UI      │
                     └───────────────┬───────────────┘
                                     │ triggers
                     ┌───────────────▼───────────────┐
                     │  Execution Harness (Guard)    │
                     │  - max_iterations             │
                     │  - timeout_seconds (wall-clock)│
                     │  - mcp_circuit_breaker        │
                     └───────────────┬───────────────┘
                                     │ controls
                     ┌───────────────▼───────────────┐
                     │       Linear DAG Pipeline     │
                     └───────────────┬───────────────┘
                                     │
     ┌───────────────────────────────┼───────────────────────────────┐
     │ 1. Ingest                     │ 2. Deduplicate                │ 3. Pre-Process
┌────▼─────────────┐          ┌──────▼────────────┐           ┌──────▼────────────┐
│ MCP Client / Mock│          │ DuckDB SHA256     │           │ TextTruncator     │
│ Ingest JobPosting│          │ Check & Insert    │           │ Strip & Limit     │
└──────────────────┘          └───────────────────┘           └──────┬────────────┘
                                                                     │
     ┌───────────────────────────────────────────────────────────────┘
     │ 4. Triage                     │ 5. Decision Tree
┌────▼─────────────┐          ┌──────▼────────────┐
│ Instructor LLM   │          │ Shortlist /       │
│ MatchEvaluation  │          │ Discard & Flush   │
└──────────────────┘          └───────────────────┘
```

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Python Packaging & Config | PEP 621 pyproject.toml with dependencies (instructor, openai, pydantic, duckdb, sqlmodel, typer, rich, mcp, pytest, pytest-asyncio) and CLI entrypoints (`jobloop`, `open-job-loop`) | M1 | ORIGINAL_REQUEST §R1, R4 |
| 2 | Pydantic Core Schemas | `JobStatus`, `MatchEvaluation`, `JobPosting`, `CandidateProfile` schemas with validation | M1 | ORIGINAL_REQUEST §R2 |
| 3 | TextTruncator | Token limiter (1,500 token limit), boilerplate/EEO stripper, length validator (>50 chars), heuristic token counter | M1 | ORIGINAL_REQUEST §R1 |
| 4 | DuckDB / SQLModel Persistence | Immediate flush per job, SHA256 hash deduplication (`ON CONFLICT (content_hash) DO NOTHING`), O(1) RAM usage, async thread offloading | M1 | ORIGINAL_REQUEST §R1, R2 |
| 5 | Local LLM Engine (Instructor) | AsyncOpenAI client with `base_url="http://localhost:11434/v1"`, `api_key="ollama"`, `instructor` mode JSON, prompt templates with `<job_posting>` XML delimiters | M2 | ORIGINAL_REQUEST §R1, R2 |
| 6 | MCP Ingestion Adapter | Model Context Protocol stdio client adapter (`BaseJobIngestionClient`, `McpJobClient`, `MockMcpJobClient` for deterministic fixture replay) | M2 | ORIGINAL_REQUEST §R1, R2 |
| 7 | Execution Harness (`LocalLoopGuard`) | `src/core/harness.py`: `max_iterations`, `timeout_seconds` catching `TimeoutError` and skipping gracefully, `mcp_circuit_breaker` (CLOSED/OPEN/HALF_OPEN) | M3 | ORIGINAL_REQUEST §R3, Acceptance |
| 8 | DAG Pipeline Orchestrator | Strict DAG: Ingestion -> Deduplication -> Pre-Processing -> Triage -> Decision Tree (Discard vs Shortlist) | M3 | ORIGINAL_REQUEST §R2 |
| 9 | Typer CLI & Rich UI | Startup ASCII art banner, live updating layout/panels, spinners, commands `run`, `banner`, `stats` | M3 | ORIGINAL_REQUEST §R4, Acceptance |
| 10 | Golden Fixtures & Local Inference Test | `fixtures/golden_jobs.json` with 3 matches and 3 mismatches against local Llama 3.2; `tests/test_local_inference.py` asserting fit_score thresholding | M4 / E2E Track | ORIGINAL_REQUEST Acceptance |
| 11 | Timeout & Circuit Breaker Verification | Test harness catching `TimeoutError` if inference exceeds `timeout_seconds` and gracefully skipping job | M4 / E2E Track | ORIGINAL_REQUEST Acceptance |
| 12 | Opaque-Box E2E Test Suite (Tiers 1-4) | Comprehensive opaque-box test suite verifying CLI, schemas, truncator, DB, LLM, harness, and end-to-end pipeline | E2E Track | Dual Track requirement |
| 13 | Final E2E Pass & Adversarial Hardening | 100% pass of E2E test suite + Tier 5 adversarial stress testing | M4 | Final Milestone requirement |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Core Foundations, Schemas & Persistence | Features 1, 2, 3, 4: pyproject.toml, Pydantic schemas, TextTruncator, DuckDB persistence & deduplication | none | DONE |
| M2 | Local LLM Engine & MCP Ingestion | Features 5, 6: Instructor + AsyncOpenAI client targeting local Ollama, prompt templates, MCP ingestion adapter & mock client | M1 | DONE |
| M3 | Execution Harness, DAG Pipeline & Rich UI | Features 7, 8, 9: `LocalLoopGuard` with timeout & circuit breaker, DAG pipeline orchestrator, Typer CLI & Rich UI | M1, M2 | DONE |
| M4 | Final Milestone: E2E Integration & Adversarial Hardening | Features 10, 11, 13: 100% E2E test pass (Tiers 1-4), golden fixture evaluation, timeout handling, Tier 5 adversarial hardening | M1, M2, M3, E2E Track | DONE |
| E2E | E2E Testing Track | Features 10, 11, 12: Independent opaque-box test infra, runner, fixtures, Tiers 1-4 test suite, publishing TEST_READY.md | none (runs in parallel) | DONE |

## Interface Contracts

### Schemas (`src/models/schemas.py`)
```python
from enum import Enum
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Dict, Any

class JobStatus(str, Enum):
    INGESTED = "INGESTED"
    DUPLICATE = "DUPLICATE"
    PREPROCESSED = "PREPROCESSED"
    TRIAGED = "TRIAGED"
    SHORTLISTED = "SHORTLISTED"
    DISCARDED = "DISCARDED"
    SKIPPED_TIMEOUT = "SKIPPED_TIMEOUT"
    ERROR = "ERROR"

class Recommendation(str, Enum):
    SHORTLIST = "SHORTLIST"
    DISCARD = "DISCARD"

class MatchEvaluation(BaseModel):
    fit_score: int = Field(ge=0, le=100, description="Fit score from 0 to 100")
    recommendation: Recommendation
    matched_skills: List[str] = Field(default_factory=list)
    missing_skills: List[str] = Field(default_factory=list)
    reasoning: str = ""

class JobPosting(BaseModel):
    id: str
    content_hash: str
    title: str
    company: str
    location: Optional[str] = None
    raw_description: str
    cleaned_description: Optional[str] = None
    status: JobStatus = JobStatus.INGESTED
    fit_score: Optional[int] = None
    recommendation: Optional[Recommendation] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
```

### TextTruncator (`src/core/truncator.py`)
```python
class TextTruncator:
    def __init__(self, max_tokens: int = 1500): ...
    def truncate(self, text: str) -> str: ...
    def estimate_tokens(self, text: str) -> int: ...
```

### Database (`src/db/repository.py`)
```python
class JobRepository:
    def __init__(self, db_path: str = "open_job_loop.duckdb"): ...
    async def initialize(self) -> None: ...
    async def is_duplicate(self, content_hash: str) -> bool: ...
    async def save_job(self, job: JobPosting) -> None: ...
    async def update_status(self, job_id: str, status: JobStatus, fit_score: Optional[int] = None) -> None: ...
    async def get_stats(self) -> Dict[str, int]: ...
```

### Local LLM Evaluator (`src/llm/evaluator.py`)
```python
class JobFitEvaluator:
    def __init__(self, base_url: str = "http://localhost:11434/v1", model: str = "llama3.2:3b"): ...
    async def evaluate_fit(self, job_description: str, candidate_profile: str) -> MatchEvaluation: ...
```

### MCP Ingestion Client (`src/mcp/client.py`)
```python
class BaseJobIngestionClient:
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def fetch_jobs(self, limit: int = 10) -> List[JobPosting]: ...
```

### Execution Harness (`src/core/harness.py`)
```python
class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

class MCPCircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_time: float = 30.0): ...
    def record_success(self) -> None: ...
    def record_failure(self) -> None: ...
    def allow_request(self) -> bool: ...

class LocalLoopGuard:
    def __init__(self, max_iterations: int = 50, timeout_seconds: float = 15.0, circuit_breaker: Optional[MCPCircuitBreaker] = None): ...
    async def run_guarded(self, step_func, *args, **kwargs): ...
```

## Code Layout
```
/Users/mauriciohelfstein/dev/open-job-loop/
├── pyproject.toml
├── src/
│   ├── __init__.py
│   ├── cli.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── truncator.py
│   │   ├── harness.py
│   │   └── pipeline.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── database.py
│   │   └── repository.py
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── client.py
│   │   ├── prompts.py
│   │   └── evaluator.py
│   ├── mcp/
│   │   ├── __init__.py
│   │   ├── client.py
│   │   └── mock_client.py
│   ├── models/
│   │   ├── __init__.py
│   │   └── schemas.py
│   └── ui/
│       ├── __init__.py
│       ├── banner.py
│       └── console.py
├── fixtures/
│   └── golden_jobs.json
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_local_inference.py
    ├── test_harness.py
    ├── test_truncator.py
    ├── test_db.py
    ├── test_pipeline.py
    └── test_cli.py
```
