# Original User Request

## 2026-10-05T02:56:12Z

# Teamwork Project Prompt — Draft

> Status: Launched.
> Goal: Craft prompt → get user approval → delegate to teamwork_preview
> Requested team: [none — teamwork routes from the description]

Build a fully autonomous, privacy-first CLI agent that executes in closed loops to discover, deduplicate, evaluate technical fit, and structure job applications using strictly local open-weight models and the Model Context Protocol (MCP).

Working directory: /Users/mauriciohelfstein/dev/open-job-loop
Integrity mode: development

## References
- https://github.com/MadsLorentzen/ai-job-search
- https://github.com/career-ops-hq/career-ops
- https://github.com/chaseai-yt/claudex-loop
- https://github.com/stickerdaniel/linkedin-mcp-server

## Requirements

### R1. Local-First Execution & Architecture
- Strict Python 3.12+ async architecture.
- All LLM calls must use the standard OpenAI Python SDK but override the `base_url` to target a local endpoint (e.g., `http://localhost:11434/v1`).
- Must use the `instructor` library for deterministic Pydantic structured outputs.
- Must implement `TextTruncator` to limit job descriptions to a safe token threshold.
- Stateful operations must be flushed to a local DuckDB / SQLModel database immediately; no large arrays kept in RAM.

### R2. Data Flow and State Machine
- Use a strict DAG for processing: Ingestion (via MCP), Deduplication (SHA256 hash in DuckDB), Pre-Processing (TextTruncator), Triage (Llama 3.2 via instructor for fit score), and Decision Tree (Discard vs. Shortlist).
- Implement specific Pydantic schemas (`JobStatus`, `MatchEvaluation`, `JobPosting`).

### R3. Execution Harness (The LocalLoopGuard)
- Implement in `src/core/harness.py`.
- Include `max_iterations`, `timeout_seconds` (timeout guards instead of token budget guards), and `mcp_circuit_breaker`.

### R4. User Interface
- Use Typer for the CLI and Rich for the UI (startup ASCII banner, live updating panels, and spinners).

## Acceptance Criteria

### Testing & Verification
- [ ] `tests/test_local_inference.py` is implemented with a `fixtures/golden_jobs.json` file.
- [ ] The test correctly evaluates 3 matches and 3 mismatches against the local Llama 3.2 instance, asserting correct `fit_score` thresholding.
- [ ] The CLI starts up and renders the predefined ASCII art banner.
- [ ] The execution harness correctly catches `TimeoutError` if inference exceeds `timeout_seconds` and gracefully skips the job.

---
*Next: when approved → delegate via invoke_subagent (see Delegation Protocol)*
