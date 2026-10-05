# Handoff Report: Survey Agent 2 (MCP, Typer CLI & Rich UI)

## 1. Observation
1. **Host Environment & Tooling**:
   - Command `python3 --version; uv --version; which uv` returned `Python 3.14.4` and `uv not found`.
   - Command `which python3.12` returned `/opt/homebrew/bin/python3.12`.
   - Command `/opt/homebrew/bin/python3.12 --version` returned `Python 3.12.13` (exited 0).
   - Command `/opt/homebrew/bin/python3.12 -c "import venv; print('venv available')"` returned `venv available` (exited 0).
2. **Local LLM Engine & Endpoints**:
   - Command `which ollama; ollama list` returned `/usr/local/bin/ollama` with installed models: `llama3.2:3b` (size 2.0GB, context length 131,072) and `llama3.2:1b` (size 1.3GB).
   - Command `curl -s http://localhost:11434/v1/models` returned HTTP 200 with JSON listing both `llama3.2:3b` and `llama3.2:1b`.
3. **Reference Analysis**:
   - `ORIGINAL_REQUEST.md` lines 24-41 detail R1 (Python 3.12+ async, standard OpenAI SDK overriding `base_url` to `http://localhost:11434/v1`, Instructor Pydantic outputs, TextTruncator, immediate DuckDB/SQLModel flush), R2 (DAG: MCP ingestion -> SHA256 dedup in DuckDB -> TextTruncator pre-processing -> Llama 3.2 triage -> Decision tree; schemas: JobStatus, MatchEvaluation, JobPosting), and R4 (Typer CLI, Rich startup ASCII banner, live updating panels, spinners).
   - `stickerdaniel/linkedin-mcp-server` README shows tools `search_jobs`, `get_job_details`, `get_saved_jobs`, `get_job_apply_url`, requiring Patchright/Playwright browser session over stdio/streamable-http.

## 2. Logic Chain
1. *From Observation 1 & 2*: The system possesses Python 3.12.13 and active Ollama running `llama3.2:3b` at `http://localhost:11434/v1`. This satisfies Requirement R1 without needing mock LLM endpoints during actual live tests, while enabling deterministic unit tests.
2. *From Observation 3*: Live MCP interaction with LinkedIn depends on a local browser cookie session which is inherently flaky in headless/automated test suites. Therefore, an `McpClientAdapter` abstraction with a companion `MockMcpClient` / `mock_server` is required to ensure 100% reproducible testing against `fixtures/golden_jobs.json` while retaining full compatibility with the official `mcp` SDK stdio client.
3. *From Instructor & Ollama capabilities*: `instructor.from_openai(client, mode=instructor.Mode.JSON)` is the most reliable mode for Llama 3.2 3B/1B models to produce structured `MatchEvaluation` models without hallucinating function signatures.
4. *From Rich & Typer UI requirements (R4)*:
   - Rich `Panel` wrapping multi-line ASCII art fulfills the banner acceptance test (`jobloop banner` and `open_job_loop.ui.banner:render_banner`).
   - Rich `Live` multi-panel display provides real-time DAG pipeline feedback without flooding terminal scrollback.
   - Immediate commit to DuckDB per job ensures zero large arrays in memory while enabling real-time metrics generation in Rich panels.

## 3. Caveats
- `uv` is not globally installed in PATH; packaging must use standard PEP 621 `pyproject.toml` with `hatchling` build backend so it works identically under `pip install -e .` or `uv`.
- Terminal live panels (`rich.live.Live`) should auto-detect TTY (`console.is_terminal`) to avoid corrupting CI stdout or piped output.

## 4. Conclusion
1. Requirements R1, R2, and R4 have been fully investigated and mapped to concrete, executable architectures documented in `/Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/explorer_survey_2/survey_mcp_ui.md`.
2. Recommended stack:
   - Packaging: PEP 621 `pyproject.toml` using `hatchling`, CLI entry points `open-job-loop` and `jobloop`.
   - LLM: `AsyncOpenAI(base_url="http://localhost:11434/v1", api_key="ollama")` wrapped with `instructor(mode=Mode.JSON)`.
   - MCP Ingestion: `mcp` SDK stdio client with `BaseJobIngestionClient` adapter supporting both live `mcp-server-linkedin` and deterministic offline mock server.
   - Persistence: `SQLModel` + `duckdb-engine` (or native DuckDB connection) with immediate per-job commit.
   - UI: `typer` + `rich.panel.Panel` ASCII banner + `rich.live.Live` pipeline telemetry.

## 5. Verification Method
1. Verify report generation:
   - File exists: `test -f /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/explorer_survey_2/survey_mcp_ui.md`
2. Verify Python 3.12 availability:
   - `/opt/homebrew/bin/python3.12 --version` (returns `Python 3.12.13`)
3. Verify Ollama Llama 3.2 availability:
   - `curl -s http://localhost:11434/v1/models | grep -q "llama3.2:3b"`
4. Invalidation conditions:
   - Ollama service terminated or models deleted.
   - Dependencies incompatible with Python 3.12.
