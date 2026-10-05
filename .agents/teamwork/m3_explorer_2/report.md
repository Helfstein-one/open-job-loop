# Engineering Report: DAG Pipeline Orchestrator Design & Specifications

- **Author**: M3 Explorer 2 (`teamwork_preview_explorer`)
- **Target Modules**: `src/core/pipeline.py`, `tests/test_pipeline.py`
- **Reference Implementations**:
  - Code: `.agents/teamwork/m3_explorer_2/proposed_pipeline.py`
  - Tests: `.agents/teamwork/m3_explorer_2/proposed_test_pipeline.py`
- **Dependencies**: `src/models/schemas.py`, `src/db/repository.py`, `src/core/truncator.py`, `src/llm/evaluator.py`, `src/mcp/client.py`, `src/core/harness.py`

---

## 1. Executive Summary

This specification delivers the architectural design, state machine contracts, fault tolerance semantics, and complete verified code implementation for the 5-Stage Directed Acyclic Graph (DAG) Pipeline Orchestrator in `src/core/pipeline.py` and its accompanying unit test suite in `tests/test_pipeline.py`.

The pipeline orchestrates closed-loop autonomous job processing:
1. **Stage 1 (Ingestion)**: Discovers job postings via `BaseJobIngestionClient` (`McpJobClient` / `MockMcpJobClient`).
2. **Stage 2 (Deduplication)**: Evaluates canonical SHA256 content hashes against DuckDB; flags duplicates (`DUPLICATE`) and halts downstream execution.
3. **Stage 3 (Pre-Processing)**: Strips boilerplate and HTML, validates minimum length (>50 chars), enforces context ceilings via `TextTruncator`, and applies XML delimiter isolation.
4. **Stage 4 (Triage)**: Evaluates technical candidate match via local Llama 3.2 inference (`JobFitEvaluator`), guarded by `LocalLoopGuard` wall-clock timeouts (`SKIPPED_TIMEOUT`).
5. **Stage 5 (Decision Tree)**: Evaluates fit score thresholding (`SHORTLISTED` vs `DISCARDED`) and flushes final state immediately to DuckDB.

---

## 2. Architectural Blueprint & State Machine

```
                              ┌───────────────────────────────────┐
                              │     MCP Client Ingestion          │ (Stage 1)
                              │  BaseJobIngestionClient           │
                              └─────────────────┬─────────────────┘
                                                │ JobPosting (status=INGESTED)
                                                ▼
                              ┌───────────────────────────────────┐
                              │      DuckDB Deduplication         │ (Stage 2)
                              │  SHA256 content_hash lookup       │
                              └─────────┬───────────────────┬─────┘
                     Duplicate Hash     │                   │ Unique Hash
                     ┌──────────────────┘                   │
                     ▼                                      ▼
            ┌─────────────────┐           ┌───────────────────────────────────┐
            │ status=DUPLICATE│           │      Text Pre-Processing          │ (Stage 3)
            │ (Skip 3, 4, 5)  │           │  TextTruncator (EEO strip, XML)   │
            └─────────────────┘           └─────────┬───────────────────┬─────┘
                                        Too Short   │                   │ Cleaned & Truncated
                     ┌──────────────────────────────┘                   │
                     ▼                                                  ▼
            ┌─────────────────┐           ┌───────────────────────────────────┐
            │  status=FAILED  │           │      Local LLM Triage             │ (Stage 4)
            │ (Skip 4, 5)     │           │  JobFitEvaluator (Llama 3.2)      │
            └─────────────────┘           │  Guarded by LocalLoopGuard        │
                                          └─────────┬───────────────────┬─────┘
                                  Timeout / LLM Err │                   │ MatchEvaluation
                     ┌──────────────────────────────┘                   │
                     ▼                                                  ▼
            ┌─────────────────┐           ┌───────────────────────────────────┐
            │ SKIPPED_TIMEOUT │           │        Decision Tree              │ (Stage 5)
            │ or ERROR        │           │  fit_score >= threshold (e.g. 70) │
            └─────────────────┘           └─────────┬───────────────────┬─────┘
                                                    │                   │
                                                    │ True              │ False
                                                    ▼                   ▼
                                          ┌─────────────────┐ ┌─────────────────┐
                                          │SHORTLISTED      │ │DISCARDED        │
                                          │rec=SHORTLIST    │ │rec=DISCARD      │
                                          └─────────────────┘ └─────────────────┘
```

---

## 3. Core Component Specifications

### 3.1 Telemetry Event System (`PipelineEventType` & `PipelineEvent`)

The telemetry subsystem provides event hooks for Typer CLI and Rich UI integration:

```python
class PipelineEventType(str, Enum):
    PIPELINE_STARTED = "PIPELINE_STARTED"
    INGESTION_STARTED = "INGESTION_STARTED"
    INGESTION_COMPLETED = "INGESTION_COMPLETED"
    JOB_INGESTED = "JOB_INGESTED"
    JOB_DUPLICATE = "JOB_DUPLICATE"
    JOB_PREPROCESSED = "JOB_PREPROCESSED"
    JOB_TRIAGE_STARTED = "JOB_TRIAGE_STARTED"
    JOB_TRIAGED = "JOB_TRIAGED"
    JOB_SHORTLISTED = "JOB_SHORTLISTED"
    JOB_DISCARDED = "JOB_DISCARDED"
    JOB_SKIPPED_TIMEOUT = "JOB_SKIPPED_TIMEOUT"
    JOB_ERROR = "JOB_ERROR"
    PIPELINE_STOPPED = "PIPELINE_STOPPED"
    PIPELINE_COMPLETED = "PIPELINE_COMPLETED"

@dataclass
class PipelineEvent:
    event_type: PipelineEventType
    job: Optional[JobPosting] = None
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    stage_number: Optional[int] = None
```

- **Callback signature**: `PipelineCallback = Callable[[PipelineEvent], Union[Awaitable[None], None]]`
- **Listener isolation**: Listeners are invoked with `try/except Exception as exc: logger.warning(...)`, preventing any UI exception from crashing the pipeline loop.

### 3.2 O(1) Memory Guarantee & Streaming Iteration

To satisfy project requirement R1:
1. **Immediate Disk Commits**: After each transition (stages 2, 3, 4, 5), `JobRepository.save_job`, `update_job`, or `update_status` flushes changes to DuckDB with WAL checkpointing.
2. **Explicit Reference Dropping**: Jobs are ingested in chunks (`batch_size=10`), processed one-by-one, and dereferenced (`del job`, `del processed_job`).
3. **Scalar Summaries**: `PipelineResult` stores only counters (`total_ingested`, `shortlisted`, `discarded`, `duplicates`, `skipped_timeout`, `errors`).
4. **Generator Support**: `JobPipeline.process_stream(job_stream: AsyncIterator[JobPosting])` enables unbounded streaming without heap inflation.

### 3.3 Harness & Timeout Guard Integration

The pipeline coordinates with `LocalLoopGuard` from `src/core/harness.py`:
- **Iteration bounding**: Checks `guard.can_continue()` before batch fetching and before job execution.
- **Inference timeout recovery**: When `JobFitEvaluator.evaluate_fit` exceeds `guard.timeout_seconds`:
  1. Catches `TimeoutError` or `LLMTimeoutError`.
  2. Sets `job.status = JobStatus.SKIPPED_TIMEOUT`.
  3. Records `job.error_message = "Triage timed out"`.
  4. Flushes status immediately to DuckDB: `await repository.update_status(job.id, status=JobStatus.SKIPPED_TIMEOUT, error_message=...)`.
  5. Emits `PipelineEventType.JOB_SKIPPED_TIMEOUT`.
  6. Gracefully skips to next job in closed loop without raising uncaught exceptions.
- **Circuit Breaker Awareness**: Inspects `guard.circuit_breaker.allow_request()` before upstream MCP fetching; catches `CircuitOpenError`, emits `PIPELINE_STOPPED`, and terminates gracefully.

---

## 4. Full Source Code Specifications

### 4.1 `src/core/pipeline.py`
The complete, production-ready code is stored at:
`/Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m3_explorer_2/proposed_pipeline.py`

Key features:
- Complete PEP 621 / Python 3.12+ syntax (`list[str]`, `str | None`).
- All 5 stages modularized: `stage_1_ingest`, `stage_2_deduplicate`, `stage_3_preprocess`, `stage_4_triage`, `stage_5_decision_tree`.
- Atomic `process_job(job)` and batch closed loop `run(limit, batch_size)`.
- Streaming `process_stream(job_stream, limit)`.
- Resilient import fallback for `LocalLoopGuard`.

### 4.2 `tests/test_pipeline.py`
The complete, production-ready unit test suite is stored at:
`/Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m3_explorer_2/proposed_test_pipeline.py`

Test inventory:
- `TestPipelineInitialization`: default configs, custom thresholds, listener management.
- `TestPipelineLifecycle`: full 5-stage happy path (SHORTLISTED) and discard path (DISCARDED).
- `TestPipelineDeduplication`: existing database hash skip, batch duplicate collision.
- `TestPipelinePreProcessing`: context ceiling truncation (>1500 tokens), short description (<50 chars) rejection.
- `TestPipelineTriageResilience`: `LLMTimeoutError` graceful skip, `LLMConnectionError` handling, `LocalLoopGuard.run_guarded` integration.
- `TestPipelineIterationBounding`: `limit` stopping, `guard.can_continue()` loop termination.
- `TestPipelineTelemetry`: sync/async callbacks, listener exception containment.
- `TestPipelineStreamingRAM`: `process_stream` async generator, DuckDB aggregation parity.

---

## 5. Verification Results

- **Linter Status**:
  ```bash
  /opt/homebrew/bin/ruff check .agents/teamwork/m3_explorer_2/proposed_pipeline.py .agents/teamwork/m3_explorer_2/proposed_test_pipeline.py
  All checks passed!
  ```
- **Test Suite Status**:
  ```bash
  .venv/bin/pytest -v .agents/teamwork/m3_explorer_2/proposed_test_pipeline.py
  ============================== 17 passed in 0.77s ==============================
  ```
- **Pass Rate**: 17 / 17 (100% pass rate).
