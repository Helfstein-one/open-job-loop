# Changelog

All notable changes to this project will be documented in this file.

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
