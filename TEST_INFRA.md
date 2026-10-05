# Test Infrastructure & Specification: open-job-loop

## 1. Overview & Architecture

`open-job-loop` implements a privacy-first, autonomous CLI agent for job discovery, deduplication, and triage using local open-weight LLMs (Llama 3.2 via Ollama) and the Model Context Protocol (MCP).

The testing infrastructure follows a rigorous **Hierarchical 4-Tier Testing Model** combined with domain-specific unit suites and golden reference fixture evaluations. All tests are designed to execute either in an offline CI mode (using fast, deterministic mock components) or an end-to-end live mode (validating against a live local Ollama daemon).

```
                      ┌──────────────────────────────────────────┐
                      │            Test Hierarchy                │
                      └────────────────────┬─────────────────────┘
                                           │
         ┌───────────────────┬─────────────┴───────┬───────────────────┐
         │                   │                     │                   │
┌────────▼─────────┐┌────────▼─────────┐  ┌────────▼─────────┐┌────────▼─────────┐
│ Tier 1: Smoke    ││ Tier 2: Component│  │ Tier 3: Pairwise ││ Tier 4: Full Loop│
│ Typer CLI, Help, ││ Truncator, DuckDB│  │ 5-Stage DAG      ││ Timeouts, Circuit│
│ ASCII Banner,    ││ Dedup, Limits,   │  │ MCP->Triage->DB  ││ Breaker, Headless│
│ Arg Validation   ││ Circuit Breaker  │  │ Live & Offline   ││ & TTY Modes      │
└──────────────────┘└──────────────────┘  └──────────────────┘└──────────────────┘
```

---

## 2. Test Hierarchy (Tiers 1–4)

### Tier 1: Opaque-Box CLI Smoke & Interface (`tests/e2e/test_tier1_smoke.py`)
- **Scope**: Command dispatch (`jobloop banner`, `jobloop stats`, `jobloop run`, `--help`).
- **Behaviors Verified**:
  - Main CLI entrypoint help rendering, exit codes (`0` on `--help`).
  - ASCII art startup banner rendering in styled, `--plain`, and versioned formats.
  - Subtitle presence (`Privacy-First Local Job Search & Triage Agent`).
  - DuckDB `stats` summary on both empty and populated databases.
  - Strict parameter boundary validations:
    - `--threshold` bounded between `0` and `100` (rejects `-10` and `150`).
    - `--limit` bounded `>= 1` (rejects `0`).
    - `--max-iterations` bounded `>= 1` (rejects `0`).
    - `--timeout` bounded `>= 0.1` (rejects `0`).

### Tier 2: Subsystem Isolation & Boundary Corner Cases (`tests/e2e/test_tier2_components.py`)
- **Scope**: Core logic components in isolation without external LLM or network calls.
- **Behaviors Verified**:
  - `TextTruncator`:
    - Strict 1,500 token ceiling enforcement (`[...Description truncated for context window ceiling...]`).
    - Boilerplate, HTML tags, EEO, Fair Chance Ordinance, and recruiter notice stripping while preserving compensation and skills.
    - Minimum content length enforcement (rejection of descriptions `<= 50` characters).
    - Closing XML tag escaping (`&lt;/job_posting&gt;`) to neutralize prompt injection breakout.
  - `DuckDB Deduplication`:
    - Canonical SHA256 content hash generation (`compute_job_hash`).
    - Whitespace and casing insensitivity across title, company, and raw description.
    - Idempotent insert behavior (`is_duplicate` returns `True`, second insert rejected).
  - `LocalLoopGuard`:
    - Strict `max_iterations` boundary check (raises `MaxIterationsReachedError` once budget reached).
    - Initialization parameter validation (`max_iterations >= 1`, `timeout_seconds > 0`).
  - `MCPCircuitBreaker`:
    - Complete state transition cycle: `CLOSED` -> `OPEN` (after 3 failures) -> `HALF_OPEN` (after recovery timeout) -> `CLOSED` (on probe success).
    - Trial probe failure in `HALF_OPEN` immediately re-trips back to `OPEN`.

### Tier 3: Cross-Feature Integration & Pairwise DAG (`tests/e2e/test_tier3_live_inference.py`)
- **Scope**: Linear 5-stage DAG pipeline chaining all components together:
  `Stage 1 (MCP Ingestion) -> Stage 2 (DuckDB Dedup) -> Stage 3 (TextTruncator) -> Stage 4 (Triage) -> Stage 5 (Decision Tree & DB Flush)`.
- **Behaviors Verified**:
  - Ingestion of 6 golden jobs via `MockMcpJobClient`.
  - Immediate persistence and state tracking in DuckDB.
  - Fit score thresholding routing matching jobs to `SHORTLISTED` and non-matching jobs to `DISCARDED`.
  - Re-running the pipeline on identical stream detects 6 duplicates and halts downstream stages.
  - Live local Llama 3.2 Ollama execution asserting expected database statuses and metrics.

### Tier 4: Real-World Resilience & Fault Tolerance (`tests/e2e/test_tier4_resilience.py`)
- **Scope**: Failure recovery, slow inference timeouts, circuit breaker protection, and CLI modes.
- **Behaviors Verified**:
  - Wall-clock timeout enforcement: slow inference exceeding `timeout_seconds` is trapped by `LocalLoopGuard`, job is updated in DuckDB to `JobStatus.SKIPPED_TIMEOUT`, and pipeline advances to next job without crashing.
  - MCP circuit breaker tripping: 3 consecutive MCP tool execution failures trip breaker to `OPEN`, blocking further calls with `CircuitOpenError`.
  - Heterogeneous closed-loop stream: pipeline processes mixed batch (matching jobs, mismatching jobs, duplicate jobs, and slow timed-out jobs) and generates accurate scalar telemetry metrics.
  - CLI execution modes: clean execution under both `--headless` and interactive TTY modes.

---

## 3. Golden Fixtures & Local Inference Suite

### Golden Jobs Fixture (`fixtures/golden_jobs.json`)
The fixture establishes ground truth for evaluating technical fit against the candidate profile:
- **Target Profile**: Senior Python / AI Systems Engineer (Python 3.12, AsyncIO, Llama 3.2, Ollama, Instructor, OpenAI SDK, DuckDB, SQLModel, MCP, Pydantic, Typer, Rich).
- **Match Jobs (3)**:
  1. `match-01-python-ai-lead`: Senior Python & AI Systems Engineer at *NeuralScale Dynamics* (`expected_decision: shortlist`, `min_expected_fit_score: 0.70`).
  2. `match-02-mcp-backend-architect`: Lead Backend Engineer (MCP & Agent Tooling) at *ContextStream* (`expected_decision: shortlist`, `min_expected_fit_score: 0.70`).
  3. `match-03-agentic-loop-developer`: Python Developer - Autonomous Loop Systems at *AgentOps Lab* (`expected_decision: shortlist`, `min_expected_fit_score: 0.70`).
- **Mismatch Jobs (3)**:
  1. `mismatch-01-legacy-java-erp`: Principal Java Spring Boot ERP Architect at *LegacyCorp Enterprise* (`expected_decision: discard`, `max_expected_fit_score: 0.35`).
  2. `mismatch-02-social-marketing`: Senior TikTok & Social Media Growth Lead at *ViralSpark Agency* (`expected_decision: discard`, `max_expected_fit_score: 0.20`).
  3. `mismatch-03-icu-nurse`: Registered Nurse - Intensive Care Unit (ICU) at *Metropolitan Health Hospital* (`expected_decision: discard`, `max_expected_fit_score: 0.10`).

### Local Inference Suite (`tests/test_local_inference.py`)
- Detects local Ollama endpoint (`http://localhost:11434`) and model `llama3.2:3b`.
- If Ollama is unavailable, cleanly skips live tests with `@pytest.mark.skipif`.
- When available:
  - Asserts that all 3 matches evaluate to `fit_score >= 70` and `recommendation == Recommendation.SHORTLIST`.
  - Asserts that all 3 mismatches evaluate to `fit_score < 70` and `recommendation == Recommendation.DISCARD`.
- Includes comprehensive offline tests ensuring schema parsing, XML delimiters, and threshold logic pass in CI environments.

---

## 4. Test Execution Guide

### Run Full Test Suite (Unit + Local Inference + E2E Tiers 1–4)
```bash
.venv/bin/pytest -v
```

### Run Only E2E Test Suite (Tiers 1–4)
```bash
.venv/bin/pytest -v tests/e2e/
```

### Run Golden Jobs Local Inference Suite
```bash
.venv/bin/pytest -v tests/test_local_inference.py
```

### Run Tier by Tier
```bash
# Tier 1: CLI Smoke
.venv/bin/pytest -v tests/e2e/test_tier1_smoke.py

# Tier 2: Components & Boundaries
.venv/bin/pytest -v tests/e2e/test_tier2_components.py

# Tier 3: Cross-Feature Integration
.venv/bin/pytest -v tests/e2e/test_tier3_live_inference.py

# Tier 4: Resilience & Fault Tolerance
.venv/bin/pytest -v tests/e2e/test_tier4_resilience.py
```

### Run Code Quality & Linter
```bash
/opt/homebrew/bin/ruff check tests/
```
