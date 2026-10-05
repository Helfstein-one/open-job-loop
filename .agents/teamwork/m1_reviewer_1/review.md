# Milestone M1 Review Report: Core Foundations, Schemas & Persistence

## Review Summary

**Verdict**: REQUEST_CHANGES

The Milestone M1 implementation establishes high-quality foundations (clean Pydantic schemas, thread-safe DuckDB repository with immediate WAL checkpoint flushing, and token bounded truncation). However, adversarial stress testing and independent execution revealed one failing test (`tests/test_adversarial_m1.py::TestTextTruncatorAdversarial::test_redos_adversarial_patterns_in_boilerplate_removal`) which uncovered a **Critical data-loss bug in TextTruncator's boilerplate removal engine**, as well as a **Major record-skipping defect in JobRepository's streaming pagination generator**.

---

## Findings

### [Critical] Finding 1: Boilerplate Regex Deletes Entire Job Descriptions When Delimited by Single Newlines
- **What**: In `TextTruncator.EEO_AND_BOILERPLATE_PATTERNS`, all regex patterns end with `.*?(?:\n\n|\Z)` and use flag `(?is)`. Because `re.DOTALL` (`?s`) is enabled, `.` matches newlines. If a job posting contains an EEO statement or recruiter disclaimer followed by single newlines (`\n`) rather than double newlines (`\n\n`), `.*?` spans across newlines and swallows all remaining text until the end of the document (`\Z`).
- **Where**: `src/core/truncator.py:38-55`
- **Why**: Catastrophic data loss. Valid job responsibilities, skills, and qualifications that appear after an EEO or agency statement are completely deleted. If the remaining text is <= 50 characters, `DescriptionTooShortError` is falsely raised (failing `test_adversarial_m1.py::test_redos_adversarial_patterns_in_boilerplate_removal`). If > 50 characters, the downstream LLM evaluates a hollowed-out job description missing all actual job requirements.
- **Proof of Failure**:
  ```python
  text = 'Job Title: SRE at TechCo.\nNotice to Recruiters: No agency resumes accepted.\nResponsibilities:\n- Maintain 99.99% uptime\n- Manage Kubernetes clusters'
  cleaned = t.clean_boilerplate(text)
  # Output: 'Job Title: SRE at TechCo.'  <-- Responsibilities completely wiped out!
  ```
- **Suggestion**:
  Remove `(?s)` / `re.DOTALL` from multiline matches, or bound boilerplate removal to line-by-line / paragraph-by-paragraph matching (e.g. `(?im)^.*?(?:we are an equal opportunity employer).*?$` or splitting by lines and filtering boilerplate lines individually).

---

### [Major] Finding 2: `JobRepository.iterate_jobs` Skips 50% of Records During Status Mutation Due to Naive Offset Pagination
- **What**: `JobRepository.iterate_jobs(status, batch_size)` uses `SELECT * FROM job_postings WHERE status = ? LIMIT ? OFFSET ?`.
- **Where**: `src/db/repository.py:322-353`
- **Why**: When a consumer iterates through jobs of a given status (e.g., `JobStatus.INGESTED`) and transitions each job to `PREPROCESSED` or `TRIAGED`, rows are removed from the filtered result set. Incrementing `offset` by `len(rows)` shifts the pagination window over the shrinking result set, causing approximately half the records to be skipped silently.
- **Proof of Failure**:
  Iterating 10 `INGESTED` jobs with `batch_size=2` while calling `update_status(job.id, JobStatus.PREPROCESSED)` inside the loop only processes jobs 0, 1, 4, 5, 8, 9 (skipping jobs 2, 3, 6, 7).
- **Suggestion**:
  Implement keyset/cursor-based pagination using the ordered tuple `(created_at, id)`:
  ```sql
  WHERE (created_at > ? OR (created_at = ? AND id > ?)) AND status = ?
  ORDER BY created_at ASC, id ASC
  LIMIT ?;
  ```
  This guarantees every record is visited exactly once regardless of whether status is mutated.

---

### [Minor] Finding 3: XML Delimiter Escaping is Case-Sensitive and Whitespace-Fragile
- **What**: `wrap_delimiters` performs string replacement: `text.replace(f"</{self.tag}>", f"&lt;/{self.tag}&gt;")`.
- **Where**: `src/core/truncator.py:154`
- **Why**: Adversarial inputs containing case variations (`</JOB_POSTING>`) or intra-tag whitespace (`</job_posting >`) bypass `str.replace`, allowing unescaped closing tags to potentially confuse prompt structure.
- **Suggestion**:
  Use `re.sub(rf"(?i)</\s*{re.escape(self.tag)}\s*>", f"&lt;/{self.tag}&gt;", text)`.

---

## Integrity Check

- **Hardcoded test results**: None detected.
- **Facade implementations**: None detected. Real implementations for all components.
- **Shortcuts / task bypasses**: None.
- **Fabricated verification outputs**: None.
- **Verdict on Integrity**: PASS. Work is genuine and substantive.

---

## Verified Claims

| Claim | Method | Result |
|---|---|---|
| Python 3.12+ async architecture | Python 3.12.13 runtime & asyncio verification | PASS |
| Pydantic v2 domain schemas | Instantiation, boundary tests, JSON serialization | PASS |
| DuckDB immediate WAL flush | Reopening DB connection without closing process | PASS |
| SHA256 atomic deduplication | `ON CONFLICT (content_hash) DO NOTHING RETURNING id` concurrent collision tests | PASS |
| Bounded token truncation | 1,500 token ceiling and natural boundary cutting | PASS |
| Test suite execution | Ran `.venv/bin/pytest -v` | **FAIL (1 failed, 80 passed)** |

---

## Coverage Gaps

- **Local Ollama Inference**: Scheduled for Milestone M2.
- **CLI Commands (`src/cli.py`)**: Scheduled for Milestone M3.

---

## Action Items for Worker

1. Fix boilerplate regexes in `src/core/truncator.py` to prevent consuming subsequent lines when delimited by single newlines `\n`.
2. Fix `JobRepository.iterate_jobs` to use keyset pagination `(created_at, id)` instead of `OFFSET` pagination.
3. Harden `wrap_delimiters` against whitespace and case variants.
4. Ensure `.venv/bin/pytest -v` runs with 100% passes (including `tests/test_adversarial_m1.py`).
