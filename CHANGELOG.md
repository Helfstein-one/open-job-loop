# Changelog

All notable changes to this project will be documented in this file.

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
