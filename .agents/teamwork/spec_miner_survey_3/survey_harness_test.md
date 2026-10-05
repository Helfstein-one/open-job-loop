# Survey Report: Execution Harness (LocalLoopGuard) & Testing/Verification Specifications

**Target Path**: `/Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/spec_miner_survey_3/survey_harness_test.md`  
**Author**: Survey Agent 3 (`teamwork_preview_spec_miner`)  
**Scope**: Requirement R3 (`LocalLoopGuard`), Acceptance Criteria (Local Inference, Golden Fixtures, ASCII Banner, Timeout Handling), and Opaque-Box E2E Test Suite Structure (Tiers 1-4).

---

## 1. Executive Summary

This survey defines the detailed engineering specifications for:
1. **The Execution Harness (`LocalLoopGuard`)** in `src/core/harness.py`: Bounding loop iterations, enforcing wall-clock timeout guards in place of token budget guards for local LLM inference, and providing resilience against MCP failures via an integrated circuit breaker.
2. **Testing & Verification Acceptance Criteria**:
   - `tests/test_local_inference.py` asserting fit_score thresholding against a live local Llama 3.2 instance using `fixtures/golden_jobs.json` with 3 matches and 3 mismatches.
   - Startup ASCII art banner rendering verification via Typer CLI runner and Rich console capture.
   - Verification that `TimeoutError` is caught by `LocalLoopGuard`, triggering graceful job skip without terminating the loop.
3. **Opaque-Box E2E Test Suite Structure**: A structured 4-tier testing hierarchy (Smoke CLI -> Isolated Subsystems -> Local System Integration -> Full Pipeline Resilience).

---

## 2. Requirement R3: Execution Harness (`The LocalLoopGuard`)

### 2.1 Philosophy: Timeout Guards vs. Token Budget Guards
In cloud-based LLM architectures, API spending is constrained by token budget guards (tracking prompt and completion tokens to avoid cloud billing runaway). In contrast, `open-job-loop` operates **strictly on local open-weight models** (e.g. Llama 3.2 via Ollama on localhost:11434). For local inference:
- Marginal token cost is zero.
- Primary failure modes are **wall-clock generation stalls, infinite decoding loops, high inference latency under GPU/CPU contention, and agent loop deadlocks**.
- Therefore, token budget guards are explicitly replaced by **wall-clock timeout guards** (`timeout_seconds`), using Python's `asyncio.timeout` / `asyncio.wait_for`.

### 2.2 Class Specification: `LocalLoopGuard` (`src/core/harness.py`)

#### Responsibilities:
1. **Loop Bounding**: Enforces `max_iterations` to prevent unbounded or runaway processing loops.
2. **Per-Job Wall-Clock Timeout Guard**: Wraps triage inference calls in `asyncio.timeout(self.timeout_seconds)`. If inference exceeds the deadline, catches `TimeoutError`, updates job record status to `SKIPPED_TIMEOUT`, logs a warning, and continues to the next item.
3. **MCP Fault Tolerance**: Uses `mcp_circuit_breaker` to wrap all external MCP tool calls (ingestion/job board queries). If an MCP server fails repeatedly, the breaker trips to prevent loop stalls.

#### Public Interface:
```python
from enum import Enum
import asyncio
from typing import Callable, Coroutine, Any, Optional
from pydantic import BaseModel

class CircuitState(str, Enum):
    CLOSED = "CLOSED"        # Normal operations: requests pass through
    OPEN = "OPEN"            # Tripped: requests fail fast or return fallback
    HALF_OPEN = "HALF_OPEN"  # Recovery testing: limited trial requests allowed

class MCPCircuitBreaker:
    """Circuit breaker guarding MCP client ingestion/queries."""
    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        half_open_success_threshold: int = 1,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_success_threshold = half_open_success_threshold
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.last_failure_time: Optional[float] = None

    def can_execute(self) -> bool: ...
    def record_success(self) -> None: ...
    def record_failure(self, error: Exception) -> None: ...
    async def call(self, func: Callable[..., Coroutine[Any, Any, Any]], *args, **kwargs) -> Any: ...

class LocalLoopGuard:
    """Execution harness enforcing iteration limits, inference timeouts, and MCP resilience."""
    def __init__(
        self,
        max_iterations: Optional[int] = None,
        timeout_seconds: float = 30.0,
        mcp_circuit_breaker: Optional[MCPCircuitBreaker] = None,
    ):
        self.max_iterations = max_iterations
        self.timeout_seconds = timeout_seconds
        self.circuit_breaker = mcp_circuit_breaker or MCPCircuitBreaker()
        self.current_iteration = 0

    def should_continue(self) -> bool:
        """Checks if max_iterations limit has been reached."""
        if self.max_iterations is not None and self.current_iteration >= self.max_iterations:
            return False
        return True

    def increment_iteration(self) -> int:
        """Increments and returns current iteration counter."""
        self.current_iteration += 1
        return self.current_iteration

    async def execute_job_guarded(
        self,
        job_handler: Callable[[Any], Coroutine[Any, Any, Any]],
        job: Any,
        on_timeout: Optional[Callable[[Any, float], Coroutine[Any, Any, None]]] = None,
    ) -> Optional[Any]:
        """
        Executes a job triage/evaluation coroutine under timeout_seconds guard.
        Catches TimeoutError; calls on_timeout hook; returns None instead of raising.
        """
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await job_handler(job)
        except TimeoutError as err:
            if on_timeout:
                await on_timeout(job, self.timeout_seconds)
            return None
```

### 2.3 Circuit Breaker Transitions
- **`CLOSED`**: Calls execute normally. On exception, increments `consecutive_failures`. If failures reach `failure_threshold` (default: 3), state transitions to `OPEN` and records `last_failure_time`.
- **`OPEN`**: Calls immediately raise `MCPCircuitBreakerOpenError` or return safe fallback (empty list). If elapsed time since `last_failure_time` exceeds `recovery_timeout` (default: 30.0s), transitions to `HALF_OPEN`.
- **`HALF_OPEN`**: Allows probe call. If successful `half_open_success_threshold` times (default: 1), transitions to `CLOSED` and resets failure count. If probe fails, immediately reverts to `OPEN`.

---

## 3. Testing & Verification Requirements (Acceptance Criteria)

### 3.1 `tests/test_local_inference.py` & `fixtures/golden_jobs.json`

#### Candidate Target Profile Specification:
To establish ground truth for testing, the system evaluates against a defined reference profile:
- **Target Role**: Senior Python / AI Systems Engineer
- **Core Skills**: Python 3.12+, AsyncIO, Local LLMs (Ollama / vLLM / Llama 3.2), Instructor, OpenAI Python SDK, DuckDB, Model Context Protocol (MCP), Pydantic, CLI tooling.
- **Experience**: 5+ years.
- **Scoring Scale**: 0.0 to 1.0 (or 0 to 100).
- **Threshold**:
  - `fit_score >= 0.70` (70%) => `JobStatus.SHORTLIST`
  - `fit_score < 0.70` (70%) => `JobStatus.DISCARD`

#### `fixtures/golden_jobs.json` Structure (6 Postings: 3 Matches, 3 Mismatches):
```json
[
  {
    "id": "match-01-python-ai-lead",
    "title": "Senior Python & AI Systems Engineer",
    "company": "NeuralScale Dynamics",
    "location": "Remote",
    "description": "Seeking a Senior Python Engineer experienced in Python 3.12, AsyncIO, and local open-weight model orchestration (Llama 3.2, Ollama). You will build agentic execution pipelines using Instructor, Pydantic structured outputs, and DuckDB immediate persistence. Must know Model Context Protocol (MCP).",
    "expected_match": true,
    "expected_decision": "shortlist",
    "min_expected_fit_score": 0.70,
    "domain": "AI / Python Engineering"
  },
  {
    "id": "match-02-mcp-backend-architect",
    "title": "Lead Backend Engineer (MCP & Agent Tooling)",
    "company": "ContextStream",
    "location": "Remote",
    "description": "Looking for a backend engineer to design Model Context Protocol (MCP) servers and clients. Requirements: Expert in Python async event loops, SQLModel/DuckDB data pipelines, and CLI utilities with Typer and Rich. Experience integrating with local LLMs.",
    "expected_match": true,
    "expected_decision": "shortlist",
    "min_expected_fit_score": 0.70,
    "domain": "AI / Backend Infrastructure"
  },
  {
    "id": "match-03-agentic-loop-developer",
    "title": "Python Developer - Autonomous Loop Systems",
    "company": "AgentOps Lab",
    "location": "Remote",
    "description": "We are creating privacy-first local CLI agents. You will implement execution harnesses with timeout guards, circuit breakers, and hash deduplication in DuckDB. Strong proficiency in Python, Instructor structured outputs, and unit testing.",
    "expected_match": true,
    "expected_decision": "shortlist",
    "min_expected_fit_score": 0.70,
    "domain": "Autonomous Agents"
  },
  {
    "id": "mismatch-01-legacy-java-erp",
    "title": "Principal Java Spring Boot ERP Architect",
    "company": "LegacyCorp Enterprise",
    "location": "On-site, Chicago",
    "description": "Seeking 12+ years experience in Java 8/11/17, Spring Boot, Hibernate, Oracle DB 11g, SOAP Web Services, and IBM WebSphere. Tasked with maintaining monolithic banking ERP systems. No Python or AI development.",
    "expected_match": false,
    "expected_decision": "discard",
    "max_expected_fit_score": 0.35,
    "domain": "Monolithic Enterprise Java"
  },
  {
    "id": "mismatch-02-social-marketing",
    "title": "Senior TikTok & Social Media Growth Lead",
    "company": "ViralSpark Agency",
    "location": "Remote",
    "description": "Manage our brand TikTok, Instagram Reels, and influencer campaigns. Requirements: 4+ years running social paid ads, copywriting, Adobe Premiere video editing, and community engagement. Non-technical role.",
    "expected_match": false,
    "expected_decision": "discard",
    "max_expected_fit_score": 0.20,
    "domain": "Marketing & Social Media"
  },
  {
    "id": "mismatch-03-icu-nurse",
    "title": "Registered Nurse - Intensive Care Unit (ICU)",
    "company": "Metropolitan Health Hospital",
    "location": "On-site, Boston",
    "description": "Requires active RN state license, BLS/ACLS certification, and 2+ years direct bedside ICU experience. Administer critical care, medication titration, patient triage, and bedside telemetry monitoring.",
    "expected_match": false,
    "expected_decision": "discard",
    "max_expected_fit_score": 0.10,
    "domain": "Healthcare Clinical Nursing"
  }
]
```

#### Verification Logic in `tests/test_local_inference.py`:
- Reads `fixtures/golden_jobs.json`.
- Connects to local endpoint (`http://localhost:11434/v1`) targeting model `llama3.2:1b` or `llama3.2:3b`.
- Invokes triage engine (patched with `instructor` for `MatchEvaluation`).
- Asserts that all 3 matches evaluate to `fit_score >= 0.70` and `decision == "shortlist"`.
- Asserts that all 3 mismatches evaluate to `fit_score < 0.70` (and `<= 0.40`) and `decision == "discard"`.
- Validates structured fields: `rationale` is non-empty string, `matched_skills` and `missing_skills` list types match Pydantic schema.

### 3.2 Startup ASCII Art Banner Verification
- Predefined ASCII banner located in `src/ui/banner.py` (e.g. `OPEN_JOB_LOOP_BANNER`).
- Rendered via Rich console on CLI invocation.
- Tested using `typer.testing.CliRunner`:
  - `runner.invoke(app, ["--help"])` or `runner.invoke(app, ["run", "--help"])`.
  - Asserts exit code == 0.
  - Verifies presence of banner signature text (`OPEN-JOB-LOOP` and subtitle) in stdout.

### 3.3 Harness Catching `TimeoutError` and Gracefully Skipping
- Test in `tests/test_harness.py`:
  - Sets up `LocalLoopGuard(timeout_seconds=0.05)`.
  - Simulates slow triage handler (`await asyncio.sleep(0.5)`).
  - Invokes `execute_job_guarded`.
  - Verifies that `TimeoutError` is intercepted.
  - Verifies that job record status is updated to `SKIPPED_TIMEOUT` or marked skipped.
  - Verifies that the guard returns `None` without raising an exception, and the processing loop advances to the next iteration.

---

## 4. Opaque-Box E2E Test Suite Structure (Tiers 1-4)

Opaque-box testing treats the system as a closed boundary: verifying CLI arguments, stdout/stderr, database records, and exit codes without modifying or inspecting internal function state.

```
tests/
├── conftest.py
├── fixtures/
│   └── golden_jobs.json
├── unit/
│   ├── test_models.py
│   ├── test_truncator.py
│   └── test_dedup.py
├── test_harness.py                  # Acceptance: Harness timeout + circuit breaker
├── test_local_inference.py          # Acceptance: 3 matches / 3 mismatches on Llama 3.2
└── e2e/                             # Opaque-Box E2E Test Suite (Tiers 1-4)
    ├── test_tier1_smoke.py          # Tier 1: CLI invocation, help, banner, args
    ├── test_tier2_components.py     # Tier 2: Truncator, DuckDB dedup, loop limits
    ├── test_tier3_live_inference.py # Tier 3: Live Ollama triage & DB persistence
    └── test_tier4_resilience.py     # Tier 4: Timeout skip, circuit breaker, full loop
```

### Detailed Tier Architecture:

| Tier | Name | Target Capabilities | Test Environment & Pre-requisites | Primary Assertions |
|---|---|---|---|---|
| **Tier 1** | CLI Smoke & Interface | Typer CLI commands (`run`, `config`, `stats`), argument parsing, ASCII art banner display, invalid flags. | Zero external dependencies; in-memory / temporary configs; no Ollama required. | Exit codes (0 for valid, non-zero for invalid args); ASCII banner in stdout; `--help` documentation completeness. |
| **Tier 2** | Subsystem Isolation & Guard Verification | `TextTruncator` token thresholding, SHA256 DuckDB deduplication, `LocalLoopGuard` iteration limiting (`max_iterations`), circuit breaker state transitions (`CLOSED` -> `OPEN` -> `HALF_OPEN`). | Isolated temporary DuckDB file; mock MCP / simulated tasks; fast deterministic execution (<1s). | Duplicate jobs rejected; token limits strictly respected; loop terminates at `max_iterations`; circuit breaker trips on 3 failures. |
| **Tier 3** | Local System Integration | Live local Llama 3.2 inference with `instructor`; `fixtures/golden_jobs.json` evaluation (3 matches, 3 mismatches); DuckDB state updates. | Running Ollama at `http://localhost:11434` with model `llama3.2:1b` or `llama3.2:3b`. | Matches have `fit_score >= 0.70` & `shortlist`; mismatches have `fit_score < 0.70` & `discard`; DuckDB reflects updated statuses and rationales. |
| **Tier 4** | Full Loop Resilience & Stress | Full pipeline execution (Ingestion -> Deduplication -> Truncator -> Triage -> Decision Tree -> UI); `TimeoutError` catching with graceful job skip; MCP circuit breaker fault tolerance. | Running Ollama + mock/live MCP server + simulated slow inference. | Pipeline completes with exit code 0; timed-out job is marked `SKIPPED_TIMEOUT` and skipped; subsequent jobs processed successfully; circuit breaker prevents crash. |

---

## 5. Features Discovered

| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|----------|---------|-------------|--------|---------|----------------|----------------|
| 1 | Execution Harness | `max_iterations` Bounding | Prevents infinite loop execution by halting after a specified iteration count. | `max_iterations: int` | Boolean `should_continue()` or loop termination | Graceful exit when iteration count is reached; no unhandled exception | ORIGINAL_REQUEST.md § R3 |
| 2 | Execution Harness | `timeout_seconds` Guard | Wall-clock execution timeout per job inference instead of token budget guards. | `timeout_seconds: float` (e.g. 30.0s), async triage coroutine | Job evaluation output or `None` | Catches `TimeoutError`; does not crash; skips job gracefully | ORIGINAL_REQUEST.md § R3 & Acceptance Criteria |
| 3 | Execution Harness | `mcp_circuit_breaker` | Prevents cascading stalls by tripping when MCP queries repeatedly fail. | Consecutive failures count, recovery timeout | `can_execute() -> bool`, wrapped coroutine result | Raises `CircuitBreakerOpenError` or returns empty list when open | ORIGINAL_REQUEST.md § R3 |
| 4 | Execution Harness | Graceful Job Timeout Skipping | When LLM inference times out, records `SKIPPED_TIMEOUT` in DB and advances to next job. | Timed-out job object, DuckDB connection | Updated job record in DuckDB, next loop step | Traps `TimeoutError`, logs warning to console | ORIGINAL_REQUEST.md Acceptance Criteria |
| 5 | Testing & Verification | Golden Jobs Fixture | Reference dataset with 3 matches and 3 mismatches for candidate profile. | `fixtures/golden_jobs.json` | Parsed list of job dicts/models | Schema validation error if json is malformed | ORIGINAL_REQUEST.md Acceptance Criteria |
| 6 | Testing & Verification | Fit Score Thresholding Assertion | Validates that local Llama 3.2 evaluates 3 matches >= 0.70 and 3 mismatches < 0.70. | Job postings from `golden_jobs.json`, Llama 3.2 model | `MatchEvaluation(fit_score, decision)` | Fails pytest assertion if scores violate threshold | ORIGINAL_REQUEST.md Acceptance Criteria |
| 7 | Testing & Verification | Startup ASCII Art Banner Rendering | Displays custom ASCII banner on CLI startup using Typer & Rich. | CLI invocation (`--help` or `run`) | ASCII art in stdout/console output | CliRunner captures stderr/stdout; asserts banner string match | ORIGINAL_REQUEST.md § R4 & Acceptance Criteria |
| 8 | Test Suite Architecture | Tier 1 Opaque-Box Smoke Tests | Verifies CLI commands, argument flags, help screens, and banner display. | CLI command strings, dummy options | Process exit code (0 or 2), stdout strings | Asserts expected usage output on invalid inputs | Dispatch Prompt & E2E Protocol |
| 9 | Test Suite Architecture | Tier 2 Subsystem Opaque-Box Tests | Tests TextTruncator token limits, DuckDB SHA256 deduplication, and LoopGuard limits in isolation. | Large text payloads, duplicate job dicts, iteration limits | Truncated strings, unique DB entries, iteration count | Asserts deduplication drops duplicates, truncator keeps token budget | Dispatch Prompt & E2E Protocol |
| 10 | Test Suite Architecture | Tier 3 Live System Integration | Validates local Llama 3.2 inference via instructor and immediate DuckDB persistence. | Live Ollama endpoint, golden job postings | Evaluated DB records with `shortlist`/`discard` | Pytest skips gracefully if Ollama is not running in offline mode | Dispatch Prompt & E2E Protocol |
| 11 | Test Suite Architecture | Tier 4 Closed Loop & Resilience | End-to-end autonomous loop executing ingestion, deduplication, triage, timeout skips, and circuit breaker. | Ingestion stream containing slow/failing items | Full pipeline summary stats, final DB state | Recovers from injected timeouts and MCP failures without terminating | Dispatch Prompt & E2E Protocol |
| 12 | Persistence & State | `JobStatus.SKIPPED_TIMEOUT` | Dedicated status state to record jobs that were skipped due to inference timeout. | Job ID, timeout timestamp | DuckDB row updated with status `SKIPPED_TIMEOUT` | Handled cleanly in decision tree and summary reporting | ORIGINAL_REQUEST.md § R1, R2, R3 |

---

## 6. Edge Cases

| # | Feature | Input | Observed Behavior / Expected Requirement |
|---|---------|-------|-------------------------------------------|
| 1 | `LocalLoopGuard.timeout_seconds` | Local Llama 3.2 takes 45s on complex prompt when `timeout_seconds=30` | `asyncio.timeout(30.0)` triggers `TimeoutError`. Guard intercepts exception, logs Rich warning panel, marks job `SKIPPED_TIMEOUT` in DuckDB, and loop continues to next job. |
| 2 | `LocalLoopGuard.max_iterations` | User sets `max_iterations=0` | Loop terminates immediately before processing any jobs; returns cleanly with exit code 0 and summary indicating 0 processed. |
| 3 | `mcp_circuit_breaker` | MCP server process crashes 3 times consecutively | Consecutive failures reach threshold (3). State switches to `OPEN`. Subsequent ingestion attempts fail fast without waiting for process restart. After 30s recovery timeout, switches to `HALF_OPEN`. |
| 4 | `mcp_circuit_breaker` | In `HALF_OPEN` state, trial probe fails | Immediately transitions back to `OPEN` and resets recovery cooldown timer. |
| 5 | `golden_jobs.json` Evaluation | Job description contains ambiguous qualifications (e.g. generalist software engineer) | Fit score might fall in borderline zone (e.g. 0.50 - 0.65). Golden test cases must use unambiguous, polarized criteria (3 clear senior Python/AI matches, 3 clear non-matching domains). |
| 6 | ASCII Banner Rendering | Terminal window width is narrower than ASCII banner (e.g. 40 columns) | Rich console wrapping could distort raw ASCII. Banner should use responsive rendering or format with `overflow="ignore"` or safe fallback to prevent formatting errors. |
| 7 | Ollama Endpoint Down | `http://localhost:11434` connection refused when running `test_local_inference.py` | Pytest fixture checks connection health before running live inference test; emits informative error or uses `@pytest.mark.skipif` when offline. |
| 8 | Multiple Consecutive Timeouts | All jobs in queue exceed `timeout_seconds` | Harness skips each job sequentially without deadlock. DB reflects all as `SKIPPED_TIMEOUT`. Run summary reports `processed=N, skipped=N, shortlisted=0, discarded=0`. |
| 9 | Zero Ingested Jobs | MCP server returns empty list `[]` | Pipeline gracefully finishes loop without errors; reports "0 jobs discovered". |

---

## 7. Concrete Implementation Contracts

### 7.1 Harness Contract (`src/core/harness.py`)
- Export `LocalLoopGuard`, `MCPCircuitBreaker`, `CircuitState`, `CircuitBreakerOpenError`.
- Factory function `create_loop_guard(config: Settings) -> LocalLoopGuard`.
- Direct integration with DuckDB session/connection for recording `SKIPPED_TIMEOUT`.

### 7.2 Golden Fixtures Contract (`fixtures/golden_jobs.json`)
- Path: `fixtures/golden_jobs.json`.
- Must contain an array of 6 JSON objects matching schema:
  - `id`: string
  - `title`: string
  - `company`: string
  - `location`: string
  - `description`: string
  - `expected_match`: bool
  - `expected_decision`: "shortlist" | "discard"
  - `min_expected_fit_score` / `max_expected_fit_score`: float

### 7.3 Test Suite Layout Contract
- `tests/test_harness.py`: unit & isolated behavior for loop iterations, timeout catching, circuit breaker state machine.
- `tests/test_local_inference.py`: verification of Ollama Llama 3.2 inference using `fixtures/golden_jobs.json`.
- `tests/e2e/test_tier1_smoke.py`: Typer CLI invocation, `--help`, and ASCII banner verification.
- `tests/e2e/test_tier2_components.py`: TextTruncator, DuckDB SHA256 dedup, LoopGuard limits.
- `tests/e2e/test_tier3_live_inference.py`: Live triage and DB state transitions.
- `tests/e2e/test_tier4_resilience.py`: Timeout skip under load, circuit breaker tripping, full closed-loop run.
