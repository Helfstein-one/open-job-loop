# Handoff Report — Milestone M3: DAG Pipeline Orchestrator Design

## 1. Observation

- **Baseline Test Suite**:
  Ran `.venv/bin/pytest -v` across existing repository; 197 tests passed:
  ```
  ============================= 197 passed in 19.89s =============================
  ```
- **Existing Dependencies Inspected**:
  - `src/models/schemas.py`: `JobPosting`, `JobStatus` (includes `INGESTED`, `DUPLICATE`, `PREPROCESSED`, `TRIAGED`, `SHORTLISTED`, `DISCARDED`, `SKIPPED_TIMEOUT`, `ERROR`, `FAILED`), `Recommendation`, `MatchEvaluation`, `CandidateProfile`.
  - `src/db/repository.py`: `JobRepository` with `initialize()`, `is_duplicate()`, `save_job()`, `update_status()`, `update_job()`, `get_stats()`, `iterate_jobs()`, and `compute_job_hash()`.
  - `src/core/truncator.py`: `TextTruncator` with `process()`, `validate_length()`, `DescriptionTooShortError`.
  - `src/llm/evaluator.py`: `JobFitEvaluator` with `evaluate_fit()`, `LLMTimeoutError` inheriting from `TimeoutError`, `LLMConnectionError`.
  - `src/mcp/client.py` & `src/mcp/mock_client.py`: `BaseJobIngestionClient`, `MockMcpJobClient`.
  - `.agents/teamwork/m3_explorer_1/proposed_harness.py`: `LocalLoopGuard` with `max_iterations`, `timeout_seconds`, `run_guarded()`, `MCPCircuitBreaker`, `CircuitOpenError`, `MaxIterationsReachedError`.
- **Implemented Proposed Artifacts**:
  - `.agents/teamwork/m3_explorer_2/proposed_pipeline.py` (893 lines):
    - `JobPipeline`: Full 5-stage DAG orchestrator (`stage_1_ingest`, `stage_2_deduplicate`, `stage_3_preprocess`, `stage_4_triage`, `stage_5_decision_tree`, `process_job`, `run`, `process_stream`).
    - Immediate DuckDB writes and checkpoints per stage ($O(1)$ RAM guarantee).
    - `LocalLoopGuard` iteration bounding and inference timeout recovery (`SKIPPED_TIMEOUT`).
    - Telemetry event emitter with sync/async callback support and listener error isolation (`PipelineEventType`, `PipelineEvent`, `PipelineCallback`, `PipelineResult`).
  - `.agents/teamwork/m3_explorer_2/proposed_test_pipeline.py` (724 lines):
    - 17 unit tests covering initialization, 5-stage lifecycle, deduplication, pre-processing truncation, short description failures, timeout recovery, connection errors, guard bounding, circuit breaker pauses, sync/async telemetry, and streaming generator processing.
- **Verification Commands Executed**:
  - Ruff linter:
    ```bash
    /opt/homebrew/bin/ruff check .agents/teamwork/m3_explorer_2/proposed_pipeline.py .agents/teamwork/m3_explorer_2/proposed_test_pipeline.py
    All checks passed!
    ```
  - Pytest unit tests:
    ```bash
    .venv/bin/pytest -v .agents/teamwork/m3_explorer_2/proposed_test_pipeline.py
    ============================== 17 passed in 0.77s ==============================
    ```

## 2. Logic Chain

1. **User Requirement R1 & R2**: Requires a strict 5-stage DAG orchestrator: Ingestion -> Deduplication (SHA256 in DuckDB) -> Pre-Processing (TextTruncator) -> Triage (Llama 3.2 via instructor) -> Decision Tree (Shortlist vs Discard), with stateful operations flushed to DuckDB immediately and no large arrays in RAM.
2. **Deduplication Logic**: `stage_2_deduplicate` computes canonical SHA256 hex digest via `compute_job_hash` and checks DuckDB via `JobRepository.is_duplicate`. If exists, flags `JobStatus.DUPLICATE`, emits `JOB_DUPLICATE`, and skips stages 3-5. If unique, saves atomically via `JobRepository.save_job` and proceeds.
3. **Pre-Processing Logic**: `stage_3_preprocess` strips EEO/HTML boilerplate, truncates to context ceiling, wraps in `<job_posting>`, and updates DuckDB. Descriptions <= 50 characters raise `DescriptionTooShortError`, marking `JobStatus.FAILED`, persisting error message, and halting downstream triage.
4. **Triage Resilience (R3)**: `stage_4_triage` delegates inference to `JobFitEvaluator.evaluate_fit`, guarded by `LocalLoopGuard`. If inference exceeds `timeout_seconds`, `TimeoutError` or `LLMTimeoutError` is caught, `job.status` is set to `JobStatus.SKIPPED_TIMEOUT`, DuckDB is updated immediately, `JOB_SKIPPED_TIMEOUT` is emitted, and the pipeline gracefully skips without crashing.
5. **Decision Tree Logic**: `stage_5_decision_tree` evaluates `job.fit_score >= score_threshold` (default 70). Matching jobs are marked `SHORTLISTED` (`Recommendation.SHORTLIST`); others are marked `DISCARDED` (`Recommendation.DISCARD`), committed immediately to DuckDB, and emitted via telemetry.
6. **$O(1)$ RAM Streaming**: Jobs are pulled in configurable chunks (`batch_size=10`), processed sequentially, committed to DuckDB, and dereferenced. `PipelineResult` tracks scalar metrics only.
7. **Interoperability**: The pipeline imports from `src.core.harness` when available, falling back gracefully to standalone execution while retaining full interface compatibility with M3 Explorer 1's `proposed_harness.py`.

## 3. Caveats

- **Mock LLM vs Live Ollama**: Unit tests in `proposed_test_pipeline.py` use mock evaluators (`AsyncMock`) for deterministic CI/offline execution. Live Ollama inference against `llama3.2:3b` is tested in `tests/test_llm.py` and milestone M4 golden jobs.
- **Physical Installation Location**: As an explorer in read-only investigation mode, source files are stored in `.agents/teamwork/m3_explorer_2/proposed_pipeline.py` and `proposed_test_pipeline.py` ready for worker implementation in `src/core/pipeline.py` and `tests/test_pipeline.py`.

## 4. Conclusion

The architecture, code design, and specifications for `src/core/pipeline.py` and `tests/test_pipeline.py` are complete, production-ready, 100% lint-clean, and validated by 17 passing unit tests. All 5 DAG stages, DuckDB persistence contracts, timeout skipping guarantees, iteration limits, and UI telemetry hooks are fully satisfied.

## 5. Verification Method

To independently reproduce verification:
```bash
# 1. Run linter on proposed pipeline and test suite
/opt/homebrew/bin/ruff check .agents/teamwork/m3_explorer_2/proposed_pipeline.py .agents/teamwork/m3_explorer_2/proposed_test_pipeline.py

# 2. Run unit tests on proposed pipeline
.venv/bin/pytest -v .agents/teamwork/m3_explorer_2/proposed_test_pipeline.py

# 3. Verify entire repository regression suite remains green
.venv/bin/pytest -v tests/
```
Expected output:
- Ruff: `All checks passed!`
- Proposed tests: `17 passed in <1s`
- Full suite: `197 passed in <20s`
