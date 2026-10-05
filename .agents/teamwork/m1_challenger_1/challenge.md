# Milestone M1 Adversarial Challenge Report

**Date**: 2026-10-05  
**Reviewer**: M1 Challenger 1 (teamwork_preview_challenger)  
**Overall Risk Assessment**: HIGH  
**Verdict**: **REQUEST_CHANGES**

---

## 1. Challenge Summary

Milestone M1 establishes the core foundations: PEP 621 packaging, DuckDB persistence, Pydantic domain models, and `TextTruncator`.

While basic happy paths, boundary validations, and basic token truncation pass the existing 41 tests, empirical stress testing and adversarial payloads identified **two high-severity defects** and **two medium/low defects** that must be resolved prior to proceeding with Milestone M2:
1. **Critical Data Destruction Bug in `TextTruncator.clean_boilerplate`**: When job postings use single newlines (`\n`) or minified HTML, regexes ending with `.*?(?:\n\n|\Z)` with `(?s)` flag consume and erase all text from the boilerplate statement to the end of the document (`\Z`), wiping out actual job requirements, tech stacks, and salaries.
2. **Prompt Injection Bypass in `TextTruncator.wrap_delimiters`**: Case and whitespace variations (`</JOB_POSTING>`, `</job_posting >`, `< /job_posting>`) bypass `wrap_delimiters` literal replacement completely, allowing trivial delimiter breakout in LLM prompts.
3. **Runtime Reflection Crash in `JobRepository`**: Missing `Tuple` import in `src/db/repository.py` line 366 causes `typing.get_type_hints` to crash with `NameError: name 'Tuple' is not defined`.
4. **LLM Schema Fragility in `MatchEvaluation`**: Passing `null` (`None`) for `matched_skills` or `missing_skills` or lowercase `"shortlist"` triggers a fatal `ValidationError`.

---

## 2. Empirical Challenges & Findings

### [Critical] Challenge 1: Silent Text Deletion in `clean_boilerplate` on Single Newlines & Minified HTML

- **Assumption Challenged**: Boilerplate clauses are always isolated in double-newline paragraphs (`\n\n`) at the end of the text.
- **Root Cause**:
  In `src/core/truncator.py`, lines 38–55:
  ```python
  EEO_AND_BOILERPLATE_PATTERNS = [
      r"(?is)\b(?:we are (?:an? )?equal opportunity employer|equal (?:employment )?opportunity|eeo/aa|affirmative action employer).*?(?:\n\n|\Z)",
      ...
  ]
  ```
  Every pattern uses `(?is)` (`re.IGNORECASE` + `re.DOTALL`) and matches up to `(?:\n\n|\Z)`.
  When a job posting does not contain `\n\n` downstream (e.g. single-newline formatting common in scrapers, markdown, or MCP outputs), `.*?` matches across all newlines to `\Z`.
  Furthermore, line 119: `self._html_breaks.sub("\n", cleaned)` replaces `<p>`, `<div>`, `<br>` with a single `\n` instead of `\n\n`, exacerbating the issue on minified HTML.
- **Empirical Demonstration** (`tests/test_adversarial_m1.py::test_bug_boilerplate_catastrophic_overstripping_on_single_newlines`):
  ```python
  raw = (
      "About Acme Corp:\n"
      "We are an equal opportunity employer.\n"
      "Role: Senior Distributed Systems Architect\n"
      "Tech Stack: Python 3.12, DuckDB, AsyncIO, Redis, Docker\n"
      "Salary: $190,000 - $240,000 USD\n"
      "Responsibilities: Architect high-throughput event queues."
  )
  cleaned = truncator.clean_boilerplate(raw)
  assert cleaned == "About Acme Corp:"  # All requirements, stack, and salary WIPED OUT!
  ```
- **Blast Radius**: Massive data loss on scraped/ingested jobs. Resulting text is either falsely rejected by `validate_length` (< 50 chars) or sent to the LLM stripped of all requirements, causing triage failures.
- **Mitigation**:
  1. In `_html_breaks.sub`, replace block tags with `\n\n` rather than `\n`.
  2. Redesign `EEO_AND_BOILERPLATE_PATTERNS` to not use DOTALL across arbitrary lines. Match line-by-line or paragraph-bounded sentences without consuming subsequent requirement sections.

---

### [High] Challenge 2: Prompt Injection Delimiter Bypass via Tag Variations

- **Assumption Challenged**: Adversarial input only attempts closing delimiters with exact lowercase `</job_posting>`.
- **Root Cause**:
  In `src/core/truncator.py`, line 154:
  ```python
  sanitized = text.replace(f"</{self.tag}>", f"&lt;/{self.tag}&gt;")
  ```
  `str.replace` is strict and case-sensitive.
- **Empirical Demonstration** (`tests/test_adversarial_m1.py::test_vulnerability_prompt_injection_case_and_whitespace_bypass`):
  Payloads such as `</JOB_POSTING>`, `</Job_Posting>`, `</job_posting >`, `< /job_posting>`, and `</job_posting\n>` are completely unescaped and present verbatim in the output of `truncate()`.
  In LLMs (Llama 3, Claude, GPT), case-insensitive and whitespace-tolerant XML parsing triggers early tag closure and execution of injected system instructions.
- **Blast Radius**: Attackers embedding hostile job postings can escape the delimiter boundary and override LLM instructions (e.g. forcing `fit_score: 100`, `SHORTLIST`).
- **Mitigation**:
  Use regex sanitization:
  ```python
  sanitized = re.sub(
      rf"<\s*/\s*{re.escape(self.tag)}\s*>",
      f"&lt;/{self.tag}&gt;",
      text,
      flags=re.IGNORECASE,
  )
  ```

---

### [Medium] Challenge 3: Missing `Tuple` Import in `src/db/repository.py`

- **Assumption Challenged**: Type annotations under `from __future__ import annotations` are always valid types.
- **Root Cause**:
  In `src/db/repository.py`, line 366:
  ```python
  def _row_to_job(self, cols: List[str], row: Tuple[Any, ...]) -> JobPosting:
  ```
  `Tuple` is used in the signature, but line 13 only imports:
  ```python
  from typing import Any, AsyncIterator, Dict, List, Optional
  ```
- **Empirical Demonstration** (`tests/test_adversarial_m1.py::test_bug_repository_missing_tuple_import_type_hints`):
  ```python
  typing.get_type_hints(JobRepository._row_to_job)
  # Raises: NameError: name 'Tuple' is not defined
  ```
- **Blast Radius**: Any framework performing runtime reflection (FastAPI, Typer CLI, Pydantic type resolvers, inspection tools) crashes with `NameError`.
- **Mitigation**:
  Add `Tuple` to the `typing` import list in `src/db/repository.py` line 13.

---

### [Low / Medium] Challenge 4: Schema Inflexibility on LLM `null` and Case Variations

- **Assumption Challenged**: Local LLM structured output always populates lists as `[]` (never `null`) and enums in strict uppercase.
- **Root Cause**:
  In `src/models/schemas.py`:
  `matched_skills: List[str] = Field(default_factory=list)`
  `recommendation: Recommendation`
  Pydantic v2 rejects `None` with `ValidationError` for `List[str]`. It also rejects lowercase `"shortlist"`.
- **Empirical Demonstration** (`tests/test_adversarial_m1.py::test_match_evaluation_null_skills_validation_error`):
  ```python
  MatchEvaluation.model_validate({
      "fit_score": 80,
      "recommendation": "SHORTLIST",
      "matched_skills": None,
      "missing_skills": None,
  })
  # Raises ValidationError: Input should be a valid list
  ```
- **Blast Radius**: Local LLMs (Ollama/Llama 3/Mistral via Instructor) frequently output `null` for empty list fields. This triggers unnecessary retries or failed extractions during pipeline execution.
- **Mitigation**:
  Add a `mode="before"` field validator or wrap in `Optional[List[str]] = Field(default_factory=list)` with normalization to empty list, and normalize enum string case.

---

## 3. Stress Test Results Summary

Full test suite executed via `.venv/bin/pytest -v`:
- Total tests: **70** (41 baseline + 29 adversarial stress tests)
- Baseline tests: **41 PASSED**
- Adversarial tests: **29 PASSED** (including empirical verifications demonstrating the exact defect behaviors)
- Test runtime: **0.40s**

| Scenario | Input / Attack | Expected Behavior | Actual Behavior | Result |
|---|---|---|---|---|
| Non-ASCII / CJK | Chinese text > 50 chars | Clean, validate, wrap | Handled correctly without crash | PASS |
| Non-ASCII / RTL | Arabic / Cyrillic text | Clean, validate, wrap | Handled correctly without crash | PASS |
| Emojis & Symbols | 🚀🐍🦆 | Preserved & counted | Handled correctly | PASS |
| Huge String (100k) | 100k chars repetition | Finish < 0.5s, truncate | Finished in 0.05s, bounded | PASS |
| Deep HTML Nesting | 5,000 nested tags | Clean without crash | Tags stripped cleanly | PASS |
| Single-Newline Job | EEO + requirements on single `\n` | Strip EEO, keep requirements | Swallowed all requirements to EOF | **DEFECT CONFIRMED** |
| Minified HTML Job | `<p>EEO</p><p>Requirements</p>` | Strip EEO, keep requirements | Swallowed all requirements to EOF | **DEFECT CONFIRMED** |
| Prompt Injection | `</JOB_POSTING>`, `</job_posting >` | Escape closing tag | Passed through unescaped | **DEFECT CONFIRMED** |
| Type Hints Introspection | `get_type_hints(JobRepository._row_to_job)` | Return hints dict | Crashes with `NameError` | **DEFECT CONFIRMED** |
| LLM Payload | `"matched_skills": null` | Default to `[]` | Crashes with `ValidationError` | **DEFECT CONFIRMED** |

---

## 4. Required Remediation for M1 Worker

To achieve approval:
1. **Fix `clean_boilerplate`**: Ensure regex patterns do not match across single newlines or swallow downstream job requirements. Update `_html_breaks` replacement to `\n\n`.
2. **Fix `wrap_delimiters`**: Replace `text.replace(...)` with case-insensitive and whitespace-tolerant regex replacement for `</tag>`.
3. **Fix `repository.py` imports**: Import `Tuple` from `typing`.
4. **Harden `MatchEvaluation`**: Allow `None` / `null` for list fields and auto-coerce to `[]`.
