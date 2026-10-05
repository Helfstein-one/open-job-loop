# BRIEFING — 2026-10-05T04:12:30Z

## Mission
Empirically probe live Ollama instance (http://localhost:11434/v1, llama3.2:3b) to test Instructor prompt templates, determine system instructions for consistent fit scores, draft 6 golden jobs for fixtures/golden_jobs.json, and measure latency/timeout.

## 🔒 My Identity
- Archetype: specification_miner
- Roles: [Teamwork specialist, Spec Miner 3]
- Working directory: /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m2_spec_miner_3
- Original parent: cff9f33a-657b-4925-9ede-c2bc2dfbaa41
- Milestone: M2

## 🔒 Key Constraints
- Local-first execution (Ollama at http://localhost:11434/v1, llama3.2:3b)
- Instructor library for deterministic Pydantic structured output (MatchEvaluation)
- Zero conversational filler (Caveman mode)
- Write-only to own directory (/Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m2_spec_miner_3)

## Current Parent
- Conversation ID: cff9f33a-657b-4925-9ede-c2bc2dfbaa41
- Updated: 2026-10-05T04:12:30Z

## Task Summary
- **What to build/probe**:
  1. Test Instructor prompt templates against live Llama 3.2 3B instance.
  2. Determine exact system instructions that produce consistent, accurate fit scores.
  3. Draft the 6 golden jobs (3 matches and 3 mismatches) for fixtures/golden_jobs.json ensuring clear score threshold separation.
  4. Measure latency and timeout characteristics.
- **Success criteria**: Empirical data on Ollama/Llama 3.2 3B instructor behavior, system prompt specification, golden jobs fixture draft, latency/timeout metrics, comprehensive report.md and handoff.md.
- **Interface contracts**: /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/orchestrator_1/PROJECT.md
- **Code layout**: PROJECT.md § Code Layout

## Key Decisions Made
- Instructor mode: `instructor.Mode.JSON` must be used. `Mode.TOOLS` fails due to Ollama stringifying array arguments.
- Security & Robustness: Chain-of-thought field ordering in Pydantic schema (`reasoning` before `fit_score`) eliminates injection vulnerabilities.
- Escaping: Strip/escape closing `</job_posting>` tags from untrusted job descriptions.
- Golden jobs separation: 3 matches (scores 85-92, SHORTLIST) vs 3 mismatches (scores 0, DISCARD) yields an 85-point margin of separation.
- Latency & Timeout: Average latency 4.51s, throughput 35.3 tps, `timeout_seconds=15.0` gives 3.3x headroom.

## Artifact Index
- /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m2_spec_miner_3/report.md — Empirical findings & specifications
- /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m2_spec_miner_3/handoff.md — Handoff report
