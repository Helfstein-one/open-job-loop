# Handoff Report: Specification Survey (Harness, Testing & Verification, E2E Tiers)

**Agent**: Survey Agent 3 (`teamwork_preview_spec_miner`)  
**Deliverables**: `/Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/spec_miner_survey_3/survey_harness_test.md`

---

## 1. Observation
- File `/Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/ORIGINAL_REQUEST.md`:
  - Lines 35-37:
    ```markdown
    ### R3. Execution Harness (The LocalLoopGuard)
    - Implement in `src/core/harness.py`.
    - Include `max_iterations`, `timeout_seconds` (timeout guards instead of token budget guards), and `mcp_circuit_breaker`.
    ```
  - Lines 44-49:
    ```markdown
    ### Testing & Verification
    - [ ] `tests/test_local_inference.py` is implemented with a `fixtures/golden_jobs.json` file.
    - [ ] The test correctly evaluates 3 matches and 3 mismatches against the local Llama 3.2 instance, asserting correct `fit_score` thresholding.
    - [ ] The CLI starts up and renders the predefined ASCII art banner.
    - [ ] The execution harness correctly catches `TimeoutError` if inference exceeds `timeout_seconds` and gracefully skips the job.
    ```
- System environment verified:
  - `ollama list` and `curl -s http://localhost:11434/api/tags`: Ollama is actively running on port 11434 with models `llama3.2:1b` (1.3 GB) and `llama3.2:3b` (2.0 GB).
  - Python runtime: Python 3.14.4 installed locally with `pytest 9.1.1`.
  - Python 3.11+ asyncio timeout verification: `asyncio.timeout(0.01)` produces standard `TimeoutError` (`isinstance(e, TimeoutError)` and `isinstance(e, asyncio.TimeoutError)` both evaluate to True).
  - Live query to `http://localhost:11434/v1/chat/completions` with `llama3.2:1b` and `llama3.2:3b` succeeded and returned completions within milliseconds when warm.

---

## 2. Logic Chain
1. *Observation 1 (R3 & Acceptance Criteria)*: Cloud token-budget guards are obsolete for local inference; failure modes are wall-clock stalls, infinite loops, and unresponsive MCP processes.
2. *Observation 2 (Asyncio Timeout Behavior)*: `asyncio.timeout(timeout_seconds)` cleanly wraps asynchronous tasks in Python 3.12+, raising standard `TimeoutError`. Catching this exception allows `LocalLoopGuard` to intercept sluggish LLM calls, log a graceful message, set `JobStatus.SKIPPED_TIMEOUT`, and continue the loop without crashing.
3. *Observation 3 (Acceptance Criteria on 3 matches / 3 mismatches)*: Testing against live local Llama 3.2 requires a deterministic reference dataset (`fixtures/golden_jobs.json`). To ensure repeatable test assertions, the 3 matching jobs must strongly match a Senior Python/AI Engineer target profile, while the 3 mismatches must represent orthogonal non-tech or non-Python domains (e.g. Java ERP, Marketing, Nursing). Thresholding at `fit_score >= 0.70` provides a clear boundary.
4. *Observation 4 (Acceptance Criteria on ASCII banner)*: Rich CLI banner rendering can be tested headlessly via `typer.testing.CliRunner`, inspecting stdout for the banner string.
5. *Observation 5 (Opaque-box E2E Hierarchy)*: Robust verification requires separating zero-dependency CLI smoke tests (Tier 1), isolated component tests (Tier 2), live local Llama 3.2 integration tests (Tier 3), and full-loop resilience tests under timeouts and circuit breaks (Tier 4).

---

## 3. Caveats
- While Ollama and Llama 3.2 instances are currently operational on `localhost:11434`, tests running in environments where Ollama is unavailable should include fallback skipping (`@pytest.mark.skipif(not is_ollama_online())`).
- Instructor must be configured with `mode=instructor.Mode.JSON` or Ollama compatible mode to enforce JSON outputs from Llama 3.2.

---

## 4. Conclusion
All functional requirements and acceptance criteria for R3, local inference verification, and test tiering are fully specified and documented in `survey_harness_test.md`. The design provides complete contracts for `LocalLoopGuard`, `MCPCircuitBreaker`, `fixtures/golden_jobs.json`, `tests/test_local_inference.py`, `tests/test_harness.py`, and the Tier 1-4 E2E suite.

---

## 5. Verification Method
1. Inspect survey findings:
   ```bash
   cat /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/spec_miner_survey_3/survey_harness_test.md
   ```
2. Verify Ollama readiness:
   ```bash
   curl -s http://localhost:11434/api/tags | grep "llama3.2"
   ```
3. Invalidation condition: If `LocalLoopGuard` re-raises `TimeoutError` or fails to transition `mcp_circuit_breaker` states upon consecutive errors, the specification contracts are violated.
