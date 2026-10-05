# Test Suite Ready: open-job-loop

**Status**: READY  
**Test Framework**: pytest (with pytest-asyncio)  
**Linter**: ruff (clean, 0 violations)  
**Total Tests**: 355 passing  
**Pass Rate**: 100%  

---

## 1. Test Suite Coverage Summary

| Test Tier | Target Scope & Capabilities | File Path | Total Tests | Pass Rate | Execution Mode |
|:---|:---|:---|:---:|:---:|:---|
| **Tier 1: Smoke** | Typer CLI commands (`banner`, `stats`, `run`, `--help`), ASCII banner rendering, option boundaries, exit codes | `tests/e2e/test_tier1_smoke.py` | 15 | 100% | Fast / Offline (<1s) |
| **Tier 2: Components** | Subsystem boundaries: `TextTruncator` (1500 tokens, boilerplate stripping, XML escaping), DuckDB SHA256 dedup, `LocalLoopGuard` iterations, `MCPCircuitBreaker` state machine | `tests/e2e/test_tier2_components.py` | 10 | 100% | Fast / Offline (<1s) |
| **Tier 3: Pairwise Integration** | 5-stage DAG pipeline (Ingest -> Dedup -> Truncate -> Triage -> Decision Tree) with `fixtures/golden_jobs.json` on both offline mock and live Llama 3.2 Ollama | `tests/e2e/test_tier3_live_inference.py` | 2 | 100% | Offline + Live Ollama |
| **Tier 4: Full Resilience** | Wall-clock `TimeoutError` catching marking `SKIPPED_TIMEOUT`, MCP circuit breaker tripping, heterogeneous stream resilience, headless & TTY CLI execution | `tests/e2e/test_tier4_resilience.py` | 5 | 100% | Offline + Mock MCP |
| **Local Inference & Golden Fixtures** | Fit score thresholding against local Llama 3.2 instance; 3 matches evaluate to `fit_score >= 70` & `SHORTLIST`, 3 mismatches evaluate to `fit_score < 70` & `DISCARD`; offline schema & mock validation | `tests/test_local_inference.py` | 7 | 100% | Live Ollama (`llama3.2:3b`) + Offline |
| **Subsystem Unit & Adversarial** | Domain models, persistence, truncator, evaluator, prompts, MCP client, harness, CLI UI layout, and stress tests | `tests/test_*.py` | 316 | 100% | Unit & Stress |
| **TOTAL** | **Comprehensive Full Test Suite** | | **355** | **100%** | |

---

## 2. Test Runner Commands

### Run Complete Test Suite (All 355 Tests)
```bash
.venv/bin/pytest -v
```

### Run Opaque-Box E2E Suite (Tiers 1–4, 32 Tests)
```bash
.venv/bin/pytest -v tests/e2e/
```

### Run Local Inference Golden Jobs Suite (7 Tests)
```bash
.venv/bin/pytest -v tests/test_local_inference.py
```

### Run Individual Tiers
```bash
# Tier 1: CLI Smoke
.venv/bin/pytest -v tests/e2e/test_tier1_smoke.py

# Tier 2: Subsystem Boundaries & Corner Cases
.venv/bin/pytest -v tests/e2e/test_tier2_components.py

# Tier 3: Cross-Feature Integration
.venv/bin/pytest -v tests/e2e/test_tier3_live_inference.py

# Tier 4: Real-World Resilience & Timeouts
.venv/bin/pytest -v tests/e2e/test_tier4_resilience.py
```

### Run Code Quality Linter
```bash
/opt/homebrew/bin/ruff check tests/
```

---

## 3. Golden Fixtures Specification (`fixtures/golden_jobs.json`)

The golden dataset contains 6 calibrated job postings (3 matches, 3 mismatches) targeting the **Senior Python / AI Systems Engineer** profile:

| Job ID | Title | Company | Domain | Expected Decision | Actual Fit Score (Llama 3.2:3b) | Status |
|:---|:---|:---|:---|:---:|:---:|:---:|
| `match-01-python-ai-lead` | Senior Python & AI Systems Engineer | NeuralScale Dynamics | AI / Python Engineering | `shortlist` | 80 | PASS |
| `match-02-mcp-backend-architect` | Lead Backend Engineer (MCP & Agent Tooling) | ContextStream | AI / Backend Infrastructure | `shortlist` | 80 | PASS |
| `match-03-agentic-loop-developer` | Python Developer - Autonomous Loop Systems | AgentOps Lab | Autonomous Agents | `shortlist` | 80 | PASS |
| `mismatch-01-legacy-java-erp` | Principal Java Spring Boot ERP Architect | LegacyCorp Enterprise | Monolithic Enterprise Java | `discard` | 0 | PASS |
| `mismatch-02-social-marketing` | Senior TikTok & Social Media Growth Lead | ViralSpark Agency | Marketing & Social Media | `discard` | 0 | PASS |
| `mismatch-03-icu-nurse` | Registered Nurse - Intensive Care Unit (ICU) | Metropolitan Health Hospital | Healthcare Clinical Nursing | `discard` | 0 | PASS |

---

## 4. Verification Checklist

- [x] `fixtures/golden_jobs.json` created with 6 golden jobs (3 matches, 3 mismatches).
- [x] `tests/test_local_inference.py` implemented with live Ollama detection and offline CI fallbacks.
- [x] `tests/e2e/` created with `test_tier1_smoke.py`, `test_tier2_components.py`, `test_tier3_live_inference.py`, `test_tier4_resilience.py`.
- [x] `TEST_INFRA.md` published at project root.
- [x] `TEST_READY.md` published at project root.
- [x] 100% pass rate across all 355 tests in pytest.
- [x] 100% clean ruff check on `tests/`.
