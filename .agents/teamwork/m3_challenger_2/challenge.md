# Adversarial Challenge Report: Milestone M3 Typer CLI & Rich UI

**Challenger**: M3 Challenger 2 (`teamwork_preview_challenger`)  
**Scope**: Typer CLI entrypoints (`jobloop`, `open-job-loop`), Rich console UI, boundary validation, non-TTY pipes, headless execution, and O(1) memory scaling.  
**Verdict**: **APPROVE**

---

## Challenge Summary

**Overall risk assessment**: **LOW**

All primary CLI and UI interfaces conform to the architecture and contracts specified in `PROJECT.md` and `ORIGINAL_REQUEST.md`. Specifically:
1. Both console script entrypoints (`jobloop` and `open-job-loop`) are operational and feature-identical across commands `banner`, `stats`, and `run`.
2. Out-of-bounds parameters (limit $\le 0$, non-numeric, float; threshold $< 0$ or $> 100$; timeout $\le 0$; max-iterations $< 1$) are deterministically rejected with exit code 2.
3. Nested DuckDB database directories are automatically created if nonexistent. Corrupted DuckDB files cause immediate fail-fast termination with non-zero exit codes.
4. Non-interactive environments and piped stdout automatically detect non-TTY status (`console.is_terminal is False`) and degrade gracefully to clean plain text and `HeadlessPipelineUI` without emitting terminal control or ANSI escape sequences.
5. High-volume workload testing (120-job batch and 150-job async streaming generator) confirmed bounded O(1) RAM consumption, maintaining peak memory usage below 0.40 MB.

One minor edge case in the underlying harness was discovered during cross-testing: `MCPCircuitBreaker.record_failure()` increments `total_trips` when already in `CircuitState.OPEN`. This does not compromise CLI stability or O(1) memory execution and is documented below for M4 hardening.

---

## Challenges

### [Low] Challenge 1: MCPCircuitBreaker Trip Inflation While Already OPEN

- **Assumption challenged**: Calling `record_failure()` while the circuit breaker is already in `CircuitState.OPEN` should not count as a new state transition or inflate the `total_trips` counter.
- **Attack scenario**: In `src/core/harness.py:183-191`, `record_failure()` increments `_failure_count` and checks `if self._failure_count >= self._failure_threshold: self._state = CircuitState.OPEN; self._total_trips += 1`. If already `OPEN`, repeated failures continuously re-increment `_total_trips`.
- **Blast radius**: Cosmetic telemetry inaccuracy in `total_trips` during consecutive failure storms. No runtime crashes or pipeline halts occur.
- **Mitigation**: Guard transition with `if prev_state == CircuitState.CLOSED and self._failure_count >= self._failure_threshold:`.

### [Low] Challenge 2: Unhandled `duckdb.IOException` Exits with Traceback Instead of Friendly Panel

- **Assumption challenged**: Providing an existing but corrupt database file should display a user-friendly error panel rather than an unhandled Python exception traceback.
- **Attack scenario**: Passing a corrupt file to `jobloop stats --db /tmp/corrupt.duckdb` or `jobloop run --db /tmp/corrupt.duckdb` raises `duckdb.IOException: The file exists, but it is not a valid DuckDB database file!`.
- **Blast radius**: Process cleanly exits with exit code 1. No data corruption occurs.
- **Mitigation**: Wrap `repo.initialize()` in `src/cli.py` with `try...except duckdb.IOException as e:` to print a styled error panel before exiting.

---

## Stress Test Results

A dedicated suite of 39 empirical stress tests was authored and executed in `tests/test_cli_ui_adversarial.py`. All tests passed (100% pass rate).

| # | Stress Scenario | Expected Behavior | Actual Behavior | Result |
|---|----------------|-------------------|-----------------|--------|
| 1 | Both binaries (`jobloop`, `open-job-loop`) exist in `.venv/bin/` | Executables present | Binaries present | PASS |
| 2 | `jobloop --help` & `open-job-loop --help` parity | Code 0, identical subcommands | Code 0, identical output | PASS |
| 3 | `jobloop banner --plain` & `open-job-loop banner --plain` parity | Code 0, ASCII art & tagline | Code 0, unstyled output | PASS |
| 4 | `jobloop stats` & `open-job-loop stats` on empty DB | Code 0, 0 records message | Code 0, 0 records message | PASS |
| 5 | `jobloop run --mock --headless` & `open-job-loop run` parity | Code 0, completed summary | Code 0, completed summary | PASS |
| 6 | Boundary limit: `--limit 0` | Code 2, range error | Code 2, range error `x>=1` | PASS |
| 7 | Boundary limit: `--limit -1`, `--limit -99` | Code 2, range error | Code 2, range error `x>=1` | PASS |
| 8 | Boundary limit: `--limit abc`, `--limit 1.5`, empty | Code 2, invalid int | Code 2, rejected | PASS |
| 9 | Boundary threshold: `--threshold -1`, `--threshold -50` | Code 2, range error | Code 2, range `0<=x<=100` | PASS |
| 10 | Boundary threshold: `--threshold 101`, `--threshold 200` | Code 2, range error | Code 2, range `0<=x<=100` | PASS |
| 11 | Boundary threshold: `--threshold xyz`, `--threshold 0.5` | Code 2, invalid int | Code 2, rejected | PASS |
| 12 | Boundary timeout: `--timeout 0`, `--timeout -5`, `--timeout abc` | Non-zero exit code | Exit code 2 | PASS |
| 13 | Boundary iterations: `--max-iterations 0`, `--max-iterations -1` | Non-zero exit code | Exit code 2 | PASS |
| 14 | Stats with nested nonexistent DB dir (`/tmp/deep/nested/path/new.duckdb`) | Auto-creates dirs & inits empty DB | Dirs created, code 0 | PASS |
| 15 | Run with nested nonexistent DB dir (`/tmp/a/b/c/run.duckdb`) | Auto-creates dirs & persists state | Dirs created, code 0 | PASS |
| 16 | Stats with corrupted DuckDB file bytes | Non-zero exit code | Code 1, `duckdb.IOException` | PASS |
| 17 | Run with corrupted DuckDB file bytes | Non-zero exit code | Code 1, `duckdb.IOException` | PASS |
| 18 | Non-TTY console auto-detection in `create_pipeline_ui` | Returns `HeadlessPipelineUI` | `isinstance` Headless | PASS |
| 19 | Interactive TTY console in `create_pipeline_ui` | Returns `LivePipelineUI` | `isinstance` Live | PASS |
| 20 | Pipe banner to `cat`: `jobloop banner \| cat` | Degrades to plain ASCII, no ANSI codes | Clean plain ASCII text | PASS |
| 21 | Pipe run to `cat` without `--headless`: `jobloop run --mock \| cat` | Auto-degrades to headless, no cursor escapes | `[HEADLESS]` log output | PASS |
| 22 | Memory scaling: 120-job batch through `JobPipeline.run()` | Bounded O(1) RAM ($\Delta < 3.0$ MB) | Peak RAM 0.37 MB ($\Delta < 0.1$ MB) | PASS |
| 23 | Memory scaling: 150-job async stream through `process_stream()` | Flat heap footprint ($\Delta < 2.0$ MB) | Heap footprint flat | PASS |
| 24 | CLI large fixture replay: `jobloop run --mock --fixture <100_jobs.json>` | Complete without memory explosion | Code 0, summary printed | PASS |

---

## Unchallenged Areas

- **Interactive ANSI Keyboard Interrupts (`Ctrl+C` / SIGINT)**: Stress-testing SIGINT signal trapping in an active pseudo-TTY subshell while Rich Live refresh loop is running requires an interactive PTY session and was deemed out of scope for automated non-interactive CI testing.
- **Local Ollama inference speed under load**: Addressed under Live Ollama validation; confirmed single-job real inference completed with fit score 80 and SHORTLIST recommendation in 6.79s.

---

## Final Recommendation

**APPROVE**. Milestone M3 Typer CLI and Rich UI meet all functional and non-functional requirements.
