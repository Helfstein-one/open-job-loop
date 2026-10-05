# Handoff Report: M3 Explorer 1 — Execution Harness Specification

## 1. Observation
- **Requirement Verification**:
  - `ORIGINAL_REQUEST.md:35-38`: Execution Harness (The LocalLoopGuard) in `src/core/harness.py` requiring `max_iterations`, `timeout_seconds`, and `mcp_circuit_breaker`.
  - `ORIGINAL_REQUEST.md:48`: "The execution harness correctly catches `TimeoutError` if inference exceeds `timeout_seconds` and gracefully skips the job."
  - `orchestrator_1/PROJECT.md:79`: `JobStatus.SKIPPED_TIMEOUT = "SKIPPED_TIMEOUT"` already declared in `src/models/schemas.py:24`.
  - `orchestrator_1/PROJECT.md:143-157`: Interface contracts for `CircuitState` (CLOSED, OPEN, HALF_OPEN), `MCPCircuitBreaker` (`failure_threshold=3`, `recovery_time=30.0`), and `LocalLoopGuard` (`max_iterations=50`, `timeout_seconds=15.0`).
- **Codebase Baseline**:
  - `src/core/harness.py` and `tests/test_harness.py` do not yet exist.
  - `src/models/schemas.py:24`: `JobStatus.SKIPPED_TIMEOUT` is verified as present and valid.
  - `src/llm/evaluator.py:41-45`: `LLMTimeoutError` inherits from `(LLMError, TimeoutError)`.
  - `tests/test_llm.py:256-259`: asserts `issubclass(LLMTimeoutError, TimeoutError)` and `isinstance(exc, TimeoutError)`.
  - `src/db/repository.py:162-200`: `JobRepository.update_status(job_id, status, error_message)` asynchronously flushes status directly to DuckDB with WAL checkpointing.
  - Virtualenv `./.venv/bin/pytest`: 197 existing tests collected and passing (19.13s execution time).

## 2. Logic Chain
1. **Circuit Breaker Mechanics**:
   - `MCPCircuitBreaker` must protect MCP stdio server calls from cascading subprocess failures.
   - States: `CLOSED` allows requests. Consecutive failures reaching `failure_threshold` trip the state to `OPEN`.
   - In `OPEN`, calls are blocked immediately (`allow_request() is False`, `call()` raises `CircuitOpenError`).
   - After `recovery_time` seconds elapse from the last failure, state transitions to `HALF_OPEN`.
   - In `HALF_OPEN`, probe requests are permitted: a success resets failure count to 0 and transitions back to `CLOSED`; any failure immediately trips back to `OPEN` and resets the recovery timer.
   - Using a monotonic `time_provider` callable allows instant, deterministic unit testing of recovery timeouts without `time.sleep()`.
2. **Loop Guard & Timeout Mechanics**:
   - `LocalLoopGuard` guards loop executions using `max_iterations` and `timeout_seconds`.
   - `can_continue()` and `check_iteration_limit()` ensure infinite loops are bounded; reaching the limit raises `MaxIterationsReachedError`.
   - `run_guarded` wraps coroutine executions within `asyncio.timeout(self.timeout_seconds)`.
   - If `TimeoutError` or `LLMTimeoutError` is raised:
     - Warning is logged with timeout duration and step identifier.
     - The target `JobPosting` entity (passed as keyword argument or detected in positional args) has its status set to `JobStatus.SKIPPED_TIMEOUT` and `error_message` populated.
     - If `JobRepository` is present, `repo.update_status(...)` is awaited immediately to guarantee immediate disk persistence ($O(1)$ RAM discipline).
     - Telemetry counters (`total_timeouts`, `total_skipped`) are incremented.
     - `run_guarded` cleanly returns `None` without crashing or propagating `TimeoutError`, allowing the outer pipeline loop to gracefully proceed to the next job.
   - Any non-timeout exception (e.g. `ValueError`, `RuntimeError`) is recorded in telemetry and re-raised to avoid swallowing programming bugs.

## 3. Caveats
- `LocalLoopGuard.run_guarded` returns `None` on timeout. Callers (such as `pipeline.py`) must check `if result is None: continue` when stepping through jobs.
- `src/core/harness.py` should be imported and re-exported in `src/core/__init__.py`.
- No modifications were written to `src/` or `tests/` pursuant to the read-only Explorer investigation role; full code specifications and test suites are documented in `report.md`.

## 4. Conclusion
Complete, robust, production-ready code designs for `src/core/harness.py` and unit tests for `tests/test_harness.py` are fully defined in `.agents/teamwork/m3_explorer_1/report.md`. The design satisfies all requirements of R3 and Acceptance Criteria in `ORIGINAL_REQUEST.md` and aligns cleanly with M3 Explorer 2 (`pipeline.py`) and M3 Explorer 3 (`cli.py`).

## 5. Verification Method
1. Implement `src/core/harness.py` according to Section 2 of `report.md`.
2. Implement `tests/test_harness.py` according to Section 3 of `report.md`.
3. Export harness symbols in `src/core/__init__.py`.
4. Run project test suite:
   ```bash
   ./.venv/bin/pytest tests/test_harness.py -v
   ./.venv/bin/pytest
   ```
5. Invalidation conditions:
   - `MCPCircuitBreaker` fails to transition from OPEN to HALF_OPEN after `recovery_time`.
   - `LocalLoopGuard.run_guarded` propagates `TimeoutError` instead of returning `None`.
   - `JobPosting.status` is not set to `JobStatus.SKIPPED_TIMEOUT` upon timeout.
   - Existing 197 tests fail or regress.
