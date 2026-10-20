# Changelog

All notable changes to this project will be documented in this file.

## [2.4.0] - 2026-10-20
### Added
- **Strict Hexagonal Architecture & SOLID Compliance (`src/application/ports/`)**:
  - Created formal abstract Port contracts:
    - `JobRepositoryPort`: Persistence, O(1) RAM streaming, and atomic SHA-256 deduplication.
    - `JobEvaluatorPort`: Candidate-job fit evaluation and structured MatchEvaluation extraction.
    - `JobIngestionPort`: Multi-source job search and normalization contract.
    - `UserProfilePort`: Semantic profile storage, DuckDB VSS cosine search, and RAG answer synthesis.
    - `AuditLoggerPort`: Application audit trail and operational logging contract.
    - `ATSAdapterPort`: Multi-channel ATS automation interface (`matches_url`, `can_handle`, `fill_and_submit`).
  - Decoupled `JobPipeline`, `AutoApplyWorker`, `ProfileManager`, and ATS adapters from concrete infrastructure classes via the Dependency Inversion Principle (DIP).
- **Multi-Query International Batch Runner (`jobloop batch-run`)**:
  - High-throughput batch rotation engine designed for 500+ jobs across US, EU, and Global markets.
  - Automated rotation of locations (`United States`, `European Union`, `Worldwide`) and keywords (`Senior Python Engineer`, `Staff Software Engineer Python`, `Senior Backend Engineer Python`) to prevent pagination truncation.
  - Pipeline cycle streaming: Discover -> Cascade Triage -> Immediate Autonomous Auto-Apply -> DuckDB Checkpoint.
- **Resilient DuckDB Concurrency & Execution Logging**:
  - `execution_logs` table and `DuckDBLoggingHandler` logging all events in real time.
  - Exponential retry loop on file lock collisions in `DatabaseManager.connect()`.
  - CLI command `jobloop logs` for live terminal log inspection.

## [2.3.0] - 2026-10-20
### Added
- **Native LinkedIn Easy Apply Filtering**:
  - `McpJobClient` defaults to `"easy_apply": True` in LinkedIn MCP searches.
  - `--easy-apply / --no-easy-apply` CLI options in `run` command (default: `True`).
- **Comprehensive DuckDB Application Audit Log**:
  - Dedicated `application_audit_log` table storing `job_id`, `job_title`, `company`, `channel`, `status`, `fields_submitted JSON`, `logs`, and `applied_at`.
  - New CLI command `jobloop audit` to inspect past submissions with resilient DuckDB connection (automatic snapshot fallback on locked files).
  - Detailed logging for completed applications, skipped external jobs, and failures.
- **100% Autonomous Zero-Confirmation Auto-Apply**:
  - Fully autonomous execution by default (`interactive=False`), removing user prompts during application runs.
  - Autonomous Active Learning auto-infers missing screening answers using local models and persists them to DuckDB VSS.
  - Robust DOM interactions in Playwright handling `<select>` dropdowns and `<fieldset>` radio buttons in Easy Apply modals.

## [2.2.0] - 2026-10-19
### Added
- **3-Tier Cascade Model Architecture (`CascadeJobEvaluator`)**:
  - **Tier 0 (Semantic Pre-check)**: DuckDB VSS cosine distance check using `nomic-embed-text` embeddings (0ms LLM time, discards completely out-of-scope roles).
  - **Tier 1 (Fast Gatekeeper)**: Ultra-fast binary classification using `llama3.2:1b` (~150ms) to discard irrelevant roles before invoking heavy inference.
  - **Tier 2 (Deep Evaluator & ATS Form Filler)**: Precision technical evaluation and structured Pydantic schema extraction via `qwen2.5-coder:3b` (or `7b` with `--deep`).
- **CLI Model Efficiency Controls**:
  - `--model-fast`: Configure Tier 1 gatekeeper model (default: `llama3.2:1b`).
  - `--model-deep`: Configure Tier 2 deep evaluator model (default: `qwen2.5-coder:3b`).
  - `--deep`: One-click switch to `qwen2.5-coder:7b` for maximum code and technical seniority reasoning.
  - `--no-cascade`: Legacy toggle to bypass cascade and run direct single-stage evaluation.
- **Testing & Performance**:
  - Test suite expanded to 439 passing tests (unit, integration, adversarial, stress, and E2E).
  - Suite execution time dropped from 3m18s to 2m46s (-16.2% latency reduction).

## [2.1.0] - 2026-10-19
### Added
- **Multi-Channel ATS Adapters**:
  - `GreenhouseAdapter`: Automated form filling and CV upload for `boards.greenhouse.io` and `grnh.se`.
  - `LeverAdapter`: Automated form filling, social profile links, and CV upload for `jobs.lever.co`.
  - `AshbyAdapter`: Form filling and custom question answering for `jobs.ashbyhq.com`.
  - `ATSRouter`: Automatic ATS detection from job URLs and redirection handling from external LinkedIn apply buttons.
- **Real-Time Active Learning Service**:
  - `ActiveLearningService`: Detects low confidence (<70%) or unmapped screening questions in real time.
  - Interactive CLI prompt for candidate input during application filling.
  - Instant vectorization and persistence of verified answers into DuckDB VSS (`user_profile` table) for continuous reinforcement.
- **Base Resume PDF Attachment**:
  - Configurable `--resume-path` / `-r` parameter in `apply` and `run` commands (default: `data/resume.pdf`).
  - Automatic file validation and attachment in Playwright file inputs across all ATS platforms.
- **Schema & Tracking Enhancements**:
  - `ApplicationChannel` enum (`LINKEDIN_EASY_APPLY`, `GREENHOUSE`, `LEVER`, `ASHBY`, `EXTERNAL_MANUAL`).
  - Automatic DuckDB migrations for `channel`, `application_log`, and `error_message` columns in `job_postings`.
  - HNSW experimental persistence handling across DuckDB connections to prevent WAL replay corruption.
  - Live dry-run validated against real LinkedIn and Ashby ATS job postings.
  - Comprehensive test suite expanded to 432 passing tests across unit, integration, adversarial, and E2E suites.

## [2.0.0] - 2026-10-19
### Added
- **Multi-Agent Architecture**:
  - `ProfilerAgent`: Profile ingestion (LinkedIn URL via Playwright or PDF export via `pypdf`), Target Job Profile formulation, and adaptive *grill-me* interview.
  - `ScoutAgent`: Job discovery through LinkedIn Model Context Protocol (MCP).
  - `EvaluatorAgent`: Technical fit scoring (0-100) with detailed match reasoning and skills breakdown via local Ollama.
  - `FormFillerAgent`: Contextualized Playwright automation for LinkedIn Easy Apply forms combining candidate VSS RAG with Evaluator match rationale.
- **DuckDB VSS Vector Engine**:
  - Semantic vector storage using `FLOAT[768]` and HNSW cosine distance indexing with `nomic-embed-text`.
  - Profile synchronization (`jobloop profile sync`), inspection (`show`), and interactive testing (`ask`).
- **Pure Native CLI Interface**:
  - New `onboard` command for automated onboarding and adaptive grill-me calibration.
  - New `login` command for interactive LinkedIn session authentication with 2FA support.
  - New `apply` command for supervised and headless Easy Apply form filling.
  - Added `--apply` and `--dry-run` options to `jobloop run`.
- **Comprehensive Testing Suite**:
  - Expanded test suite to 199 tests across `tests/unit/`, `tests/integration/`, and `tests/e2e/`.
  - Added E2E tests for Playwright Easy Apply form filling with multi-agent context.

### Changed
- Refactored entire codebase into Hexagonal Architecture (`domain`, `application`, `infrastructure`, `presentation`).
- Removed legacy Open WebUI and REST API dependencies to establish a 100% lightweight, privacy-first CLI workflow.
- Updated `pyproject.toml` with `pyyaml>=6.0.0` and `pypdf>=4.0.0`.

## [1.0.0] - Initial Release
- Initial architecture implementation with strict Python async structure.
- DuckDB state deduplication and pipeline handlers.
- Llama 3.2 local model integration via instructor.
- LocalLoopGuard for timeout monitoring and circuit breaking.
- Rich CLI UI and startup banners.
