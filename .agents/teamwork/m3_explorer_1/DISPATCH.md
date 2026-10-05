## 2026-10-05T08:17:43Z
You are M3 Explorer 1 (teamwork_preview_explorer).
Your working directory is: /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m3_explorer_1
Original Request file path: /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/ORIGINAL_REQUEST.md
Project Scope file path: /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/orchestrator_1/PROJECT.md
Dispatch file path: /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m3_explorer_1/DISPATCH.md

You MUST read /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/ORIGINAL_REQUEST.md and /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/orchestrator_1/PROJECT.md before starting work.
Task:
Develop complete, production-ready code design and specifications for:
1. `src/core/harness.py`:
   - `MCPCircuitBreaker`: CLOSED/OPEN/HALF_OPEN states, failure threshold, recovery time, reset on success.
   - `LocalLoopGuard`: `max_iterations`, `timeout_seconds`, `run_guarded` wrapping coroutines with `asyncio.timeout()`, catching `TimeoutError` (and `LLMTimeoutError`), logging, setting `JobStatus.SKIPPED_TIMEOUT`, and cleanly skipping without crashing.
2. Unit tests in `tests/test_harness.py`.

Write report to /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m3_explorer_1/report.md and handoff.md. Send message when done.
