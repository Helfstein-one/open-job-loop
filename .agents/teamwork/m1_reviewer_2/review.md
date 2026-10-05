# Milestone M1 Review & Adversarial Challenge Report

## Review Summary

**Verdict**: REQUEST_CHANGES
**Overall Risk Assessment**: HIGH

The M1 foundation code is well-structured, genuinely implemented (zero integrity violations, no facades, no hardcoded cheating), and passes its initial 41 unit tests. However, adversarial stress-testing identified two critical algorithmic defects in core components that will cause silent data loss in downstream Milestones M2 and M3:
1. `TextTruncator.clean_boilerplate()` regexes use `(?is).*?(?:\n\n|\Z)`, which matches across single newlines and completely deletes all subsequent job requirements, compensation, and tech stack details whenever boilerplate appears without double newlines.
2. `JobRepository.iterate_jobs()` uses SQL `OFFSET` pagination, which silently skips up to 40%+ of jobs when the pipeline consumer mutates job status from `INGESTED` to `PREPROCESSED` in-flight.

---

## Integrity Check
- **Hardcoded test responses in source**: None.
- **Dummy or facade implementations**: None.
- **Bypassed requirements / shortcuts**: None.
- **Fabricated verification outputs**: None.
- **Integrity Status**: PASS.

---

## Findings

### [Critical] Finding 1: Boilerplate Regex Greediness Swallows Job Content Across Single Newlines

- **What**: When EEO or boilerplate phrases appear in a job description without being followed by a double newline (`\n\n`), the regex swallows everything to `\Z` (end of document), deleting legitimate qualifications, responsibilities, and salary details.
- **Where**: `src/core/truncator.py`, lines 38-55 (`EEO_AND_BOILERPLATE_PATTERNS`) and line 119 (`_html_breaks`).
- **Why**: 
  All 9 patterns in `EEO_AND_BOILERPLATE_PATTERNS` specify `(?is)...*?(?:\n\n|\Z)`.
  In Python regex, `(?is)` activates DOTALL (`s`), so `.` matches newline characters (`\n`).
  Because `.*?` looks for `\n\n` or `\Z`, if the input text uses single newlines `\n` (standard in plain text and produced by `_html_breaks.sub("\n", cleaned)` for HTML tags like `<p>` and `<div>`), `.*?` ignores single newlines and matches all the way to `\Z`.
- **Reproducible Failure**:
  ```python
  text = """Company overview: We build AI tools. We are an equal opportunity employer.
  Key Requirements:
  - 5+ years of Python 3.12
  - Experience with DuckDB and SQLModel
  - Experience with local LLMs and Ollama"""
  cleaned = t.clean_boilerplate(text)
  # Result: 'Company overview: We build AI tools.'
  # All Key Requirements were deleted!
  ```
  In raw HTML without newlines between `<p>` tags:
  ```html
  <p>Hiring Senior Backend Engineer</p><p>We are an equal opportunity employer</p><p>Requirements: 5+ years with async Python</p>
  ```
  Result: `'Hiring Senior Backend Engineer'`. The entire requirements section is obliterated.
- **Suggestion**:
  1. In `_html_breaks`, replace block elements (`<p>`, `<div>`, `<h[1-6]>`, `<br><br>`) with `\n\n` rather than `\n`.
  2. Do not use DOTALL `(?is)` with `.*?(?:\n\n|\Z)`. Either match within paragraph lines without `s` flag (`(?i)`), or match up to `(?:\n|\Z)` or specific sentence boundaries (`(?:\.\s+|\n\n|\n|\Z)`), or match explicit recognized boilerplate clauses.

---

### [Critical] Finding 2: `JobRepository.iterate_jobs()` Silently Skips Records Under In-Flight Status Mutations

- **What**: When `iterate_jobs(status=JobStatus.INGESTED, batch_size=N)` is consumed while updating job status (e.g. `update_status(job.id, JobStatus.PREPROCESSED)`), `OFFSET` skips records.
- **Where**: `src/db/repository.py`, lines 306-353 (`iterate_jobs`).
- **Why**:
  `iterate_jobs` executes `SELECT * FROM job_postings WHERE status = ? ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?`.
  When a batch of $K$ records is fetched and their status is changed to `PREPROCESSED`, those $K$ records no longer match `WHERE status = 'INGESTED'`. The table rows matching `status = 'INGESTED'` shift towards index 0 by $K$.
  However, `iterate_jobs` then increments `offset += len(rows)`, advancing the query offset by $K$.
  This causes the next query (`OFFSET K`) to skip the next $K$ records entirely!
- **Reproducible Failure**:
  ```python
  # Insert 10 INGESTED jobs (j0 to j9)
  processed = []
  async for job in repo.iterate_jobs(status=JobStatus.INGESTED, batch_size=4):
      processed.append(job.id)
      await repo.update_status(job.id, JobStatus.PREPROCESSED)
  # Result: processed = ['j0', 'j1', 'j2', 'j3', 'j8', 'j9']
  # Jobs 'j4', 'j5', 'j6', 'j7' (40% of records) are silently skipped and never processed!
  ```
- **Suggestion**:
  Use keyset/cursor pagination:
  Track `last_id` (or `last_created_at, last_id`) and query:
  ```sql
  WHERE status = ? AND (created_at > ? OR (created_at = ? AND id > ?))
  ORDER BY created_at ASC, id ASC
  LIMIT ?
  ```
  Or, if querying a status queue being drained to another status, do not advance `offset` if rows are consumed. Keyset pagination is universally safe whether records are mutated or not.

---

### [Major] Finding 3: Missing `src/cli.py` Causes `ModuleNotFoundError` on Installed Console Scripts

- **What**: Running the console scripts declared in `pyproject.toml` crashes immediately.
- **Where**: `pyproject.toml`, lines 42-44 (`open-job-loop = "src.cli:app"`, `jobloop = "src.cli:app"`).
- **Why**: `src/cli.py` does not exist in the repository. Running `.venv/bin/jobloop --help` raises:
  `ModuleNotFoundError: No module named 'src.cli'`.
- **Suggestion**: Create a lightweight stub `src/cli.py` with a basic Typer app printing a banner or version placeholder until M3 implements full UI/CLI commands.

---

### [Minor] Finding 4: `TextTruncator.truncate()` Not Idempotent Under Composition `truncate(truncate(x))`

- **What**: Repeated truncation wraps additional `<job_posting>` delimiters and escapes existing tags.
- **Where**: `src/core/truncator.py`, line 94 (`_html_tags`), line 154 (`wrap_delimiters`).
- **Why**: `_html_tags` regex `</?[a-zA-Z][a-zA-Z0-9:-]*(?:\s+[^>]*)?>` excludes underscores `_`. Thus `<job_posting>` is not stripped as HTML, and `wrap_delimiters` escapes `</job_posting>` to `&lt;/job_posting&gt;`, adding another layer of outer tags.
- **Suggestion**: Call `strip_delimiters(text)` at the start of `clean_boilerplate` or allow underscores in tag names (`[a-zA-Z0-9:_-]`).

---

### [Minor] Finding 5: `COALESCE` in `JobRepository.update_status` Prevents Clearing `error_message`

- **What**: `COALESCE(?, error_message)` retains prior error messages when passing `error_message=None`.
- **Where**: `src/db/repository.py`, line 192.
- **Why**: If a job previously failed with `SKIPPED_TIMEOUT` and is retried successfully, passing `error_message=None` cannot nullify the previous error string in `update_status`.
- **Suggestion**: Use an explicit sentinel (e.g. `CLEAR_ERROR`) or document that full entity updates should use `update_job(job)`.

---

## Verified Claims

| Claim | Method | Result |
|---|---|---|
| 41 pytest unit tests pass | Executed `.venv/bin/pytest -v` | PASS (41 passed in 0.41s) |
| PEP 621 packaging with hatchling | Inspected `pyproject.toml` | PASS |
| Pydantic v2 schemas match `PROJECT.md` | Inspected `src/models/schemas.py` | PASS |
| DuckDB atomic deduplication (`ON CONFLICT`) | Python test script & unit tests | PASS |
| Non-blocking async DuckDB offloading | Verified `asyncio.to_thread` usage | PASS |
| Thread-safe write locks (`threading.RLock`) | Verified concurrent write test | PASS |
| SQL injection resilience | Tested SQLi in all repo inputs | PASS |
| Large text truncation performance | Tested with 490,000 characters | PASS (0.0446s) |

---

## Adversarial Stress Test Results

| Attack / Scenario | Expected Behavior | Actual Behavior | Result |
|---|---|---|---|
| EEO statement followed by single `\n` | Removes EEO, preserves rest | Deletes all text to end of string | **FAIL** (Finding 1) |
| Raw HTML without newlines between `<p>` | Preserves job requirements | Deletes all requirements | **FAIL** (Finding 1) |
| In-flight status update during `iterate_jobs` | Processes all jobs | Drops 40% of records | **FAIL** (Finding 2) |
| CLI entrypoint invocation | Shows help or version stub | `ModuleNotFoundError: src.cli` | **FAIL** (Finding 3) |
| Double truncation `truncate(truncate(x))` | Returns same output | Nests duplicate XML tags | **FAIL** (Finding 4) |
| SQL injection in title/company/description | Parameterized, no injection | Parameterized, clean | PASS |
| Multi-threaded concurrent writes | No database corruption or lock crash | Thread-safe, atomic | PASS |
| 500,000 character input | Bounded to 1,500 tokens | Bounded, 1,492 tokens | PASS |
