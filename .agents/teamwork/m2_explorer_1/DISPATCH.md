# Dispatch: M2 Explorer 1 (Local LLM Engine & Evaluator)

- Working Directory: /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m2_explorer_1
- Original Request: /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/ORIGINAL_REQUEST.md
- Project Scope: /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/orchestrator_1/PROJECT.md
- Scope: Formulate exact code specifications for:
  1. `src/llm/client.py`: AsyncOpenAI client initialization overriding `base_url="http://localhost:11434/v1"` with `api_key="ollama"`, wrapped by `instructor.from_openai(..., mode=instructor.Mode.JSON)`.
  2. `src/llm/prompts.py`: Prompt templates with `<job_posting>` XML delimiters, candidate profile context formatting, instructions forbidding instruction injection.
  3. `src/llm/evaluator.py`: `JobFitEvaluator` class evaluating `job_description` against candidate profile and returning structured `MatchEvaluation`.
  4. Unit test design for `tests/test_llm.py` (with mocks and live test hooks).

Output: Write report to `m2_explorer_1/report.md` and complete `handoff.md`.

## 2026-10-05T03:49:39Z
You are M2 Explorer 1 (teamwork_preview_explorer).
Your working directory is: /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m2_explorer_1
Original Request file path: /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/ORIGINAL_REQUEST.md
Project Scope file path: /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/orchestrator_1/PROJECT.md
Dispatch file path: /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m2_explorer_1/DISPATCH.md

You MUST read /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/ORIGINAL_REQUEST.md and /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/orchestrator_1/PROJECT.md before starting work.
Task:
Develop complete, production-ready code design and specifications for:
1. `src/llm/client.py`: AsyncOpenAI client initialization with `base_url="http://localhost:11434/v1"` and `api_key="ollama"`, wrapped by `instructor.from_openai(..., mode=instructor.Mode.JSON)`.
2. `src/llm/prompts.py`: System prompt, `<job_posting>` XML delimiters, candidate profile formatting.
3. `src/llm/evaluator.py`: `JobFitEvaluator` class evaluating `job_description` against candidate profile and returning structured `MatchEvaluation`.
4. Unit tests in `tests/test_llm.py`.

Write report to /Users/mauriciohelfstein/dev/open-job-loop/.agents/teamwork/m2_explorer_1/report.md and handoff.md. Send message when done.
