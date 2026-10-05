# Empirical Challenge Report: Milestone M3 Execution Harness & Pipeline

## Challenge Summary

**Overall risk assessment**: LOW

Empirical testing confirmed that Milestone M3 execution harness (`LocalLoopGuard`, `MCPCircuitBreaker`) and DAG pipeline orchestrator (`JobPipeline`) meet all primary functional, resilience, and contract specifications under aggressive stress testing.

1. **Extreme Timeout Recovery**: `LocalLoopGuard` cleanly bounds execution under microsecond limits (0.001s), catches both standard library `TimeoutError` and `LLMTimeoutError`, transitions job status to `JobStatus.SKIPPED_TIMEOUT`, populates descriptive error messages, updates DuckDB atomically, tracks telemetry counters, and returns `None` without crashing.
2. **MCP Circuit Breaker Resilience**: Correctly trips to `CircuitState.OPEN` upon reaching failure threshold, blocks subsequent calls with `CircuitOpenError`, transitions to `CircuitState.HALF_OPEN` strictly at `recovery_time`, recovers to `CircuitState.CLOSED` upon successful probe, and re-trips upon failed probe. Survives concurrent burst of 50 simultaneous requests in `OPEN` state.
3. **Iteration Bounding & Error Propagation**: Rejects invalid configuration (non-positive integers/floats), halts cleanly when `current_iteration >= max_iterations`, and re-raises non-timeout exceptions (`ValueError`, `RuntimeError`, `asyncio.CancelledError`) without swallowing.
4. **Pipeline Fault Tolerance**: End-to-end pipeline handles mixed batches (shortlisted, discarded, duplicates, timeouts, and short description failures) without unhandled exceptions.
5. **Streaming Memory Boundedness**: Streaming 100 jobs through `JobPipeline.process_stream` maintained bounded heap memory (< 15 MB peak RAM).

One minor metric-tracking defect was discovered in `MCPCircuitBreaker.record_failure()` when called on an already `OPEN` breaker.

---

## Challenges

### [Low] Challenge 1: `MCPCircuitBreaker.record_failure()` Metric Inflation While in `OPEN` State

- **Assumption challenged**: Calling `record_failure()` on an already `OPEN` circuit breaker should not re-trip or increment transition counters.
- **Attack scenario**: If external callers or upstream pipelines call `cb.record_failure()` after the circuit has already tripped to `OPEN` (e.g. repeated failures in rapid succession before backoff takes effect), lines 183-191 of `src/core/harness.py` execute:
  ```python
  self._failure_count += 1
  if self._failure_count >= self._failure_threshold:
      logger.warning("MCPCircuitBreaker failure threshold (%d) reached. Transitioning CLOSED -> OPEN.", self._failure_threshold)
      self._state = CircuitState.OPEN
      self._total_trips += 1
  ```
- **Blast radius**: `total_trips` counter is artificially inflated, and false "Transitioning CLOSED -> OPEN" log warnings are repeatedly emitted while the circuit was already `OPEN`. Core request-blocking functionality (`allow_request() == False` and `CircuitOpenError`) remains safe and functional.
- **Mitigation**: Add a guard clause at line 174 of `src/core/harness.py`:
  ```python
  if prev_state == CircuitState.OPEN:
      return
  ```
- **Reproduction**: Verified in `test_record_failure_while_already_open_defect_reproduction` in `tests/test_adversarial_m3_harness.py`.

---

## Stress Test Results

| Test ID | Scenario | Expected Behavior | Actual Behavior | Result |
|---|---|---|---|---|
| ST-01 | Microsecond timeout (0.001s) on slow coroutine | `TimeoutError` caught, job marked `SKIPPED_TIMEOUT`, DuckDB updated, returns `None` | Caught, marked `SKIPPED_TIMEOUT`, persisted to DuckDB, returns `None` | PASS |
| ST-02 | Explicit `LLMTimeoutError` and `TimeoutError` | Both caught and mapped to `SKIPPED_TIMEOUT` and persisted | Both caught, mapped to `SKIPPED_TIMEOUT`, DuckDB updated | PASS |
| ST-03 | DuckDB failure during timeout handling | Guard absorbs DB exception, logs error, returns `None` | DB exception logged, guard returns `None` without crashing | PASS |
| ST-04 | Timeout without JobPosting entity | Returns `None`, increments timeout telemetry | Returns `None`, telemetry updated | PASS |
| ST-05 | 10 interleaved slow/fast executions | Exactly 5 fast complete, 5 slow time out | 5 completed, 5 skipped timeouts | PASS |
| ST-06 | MCP circuit breaker threshold tripping | Trips from CLOSED to OPEN on exactly 3rd failure | State = OPEN, requests blocked, `total_trips=1` | PASS |
| ST-07 | MCP circuit breaker recovery boundary | OPEN at `recovery_time - 1ms`; HALF_OPEN at `recovery_time` | Exactly transitions to HALF_OPEN at elapsed timestamp | PASS |
| ST-08 | Half-open trial probe success | Resets state to CLOSED and clears failure count | State = CLOSED, failure count = 0 | PASS |
| ST-09 | Half-open trial probe failure | Re-trips to OPEN and increments total_trips | State = OPEN, total_trips = 2 | PASS |
| ST-10 | Requests blocked when OPEN | `CircuitOpenError` raised without invoking target | Raised immediately; target function never called | PASS |
| ST-11 | 50 concurrent requests when OPEN | All 50 concurrently receive `CircuitOpenError` | All 50 rejected cleanly | PASS |
| ST-12 | 3 continuous trip/recover cycles | Alternates CLOSED <-> OPEN <-> HALF_OPEN safely | Stable across all cycles, `total_trips=3` | PASS |
| ST-13 | Non-positive guard parameters | Raises `ValueError` for `max_iterations <= 0` and `timeout_seconds <= 0` | `ValueError` raised | PASS |
| ST-14 | Exact iteration limit exhaustion | Fails on call N+1 with `MaxIterationsReachedError` | Exactly 3 succeed; 4th raises `MaxIterationsReachedError` | PASS |
| ST-15 | Non-timeout exception propagation | `ValueError` and `RuntimeError` re-raised verbatim | Re-raised immediately, `total_errors` incremented | PASS |
| ST-16 | CancelledError propagation | `asyncio.CancelledError` not swallowed as timeout | Propagated unhindered | PASS |
| ST-17 | Pipeline halts on guard iteration limit | Halts after limit reached; emits `PIPELINE_STOPPED` | Halts after 2 jobs; emits `PIPELINE_STOPPED` | PASS |
| ST-18 | Mixed batch (shortlist, discard, dup, timeout, error) | All jobs processed according to DAG rules, DB updated | 1 shortlisted, 1 discarded, 1 dup, 1 timeout, 1 error | PASS |
| ST-19 | Streaming RAM bounded memory (100 jobs) | O(1) memory usage (< 15 MB peak heap) | Peak heap memory was ~9.2 MB (< 15 MB ceiling) | PASS |
| ST-20 | Record failure while already OPEN | `total_trips` should not inflate | Increments `total_trips` (Defect documented) | DEFECT (LOW) |

---

## Unchallenged Areas

- **Ollama Live Hardware Saturation**: Live GPU/CPU saturation during multi-gigabyte weight paging was not tested directly against local hardware, as unit tests rely on mock evaluators and `MockMcpJobClient`. Local inference with live Ollama is covered in `tests/test_local_inference.py`.
- **Multi-Process Concurrency**: Concurrency was tested across 50 asyncio coroutines within a single Python process; multi-process cross-process DuckDB lock contention was not within Milestone M3 single CLI agent scope.
