# Milestone M1 Reviewer Handoff Report

## 1. Observation

- **Test Suite Execution**: Executed `.venv/bin/pytest -v` across all 41 unit tests in `tests/test_db.py`, `tests/test_models.py`, and `tests/test_truncator.py`. All 41 passed in 0.41s with exit code 0.
- **Packaging & Entrypoints**: In `pyproject.toml`, lines 42-44 declare scripts `open-job-loop = "src.cli:app"` and `jobloop = "src.cli:app"`. Executing `.venv/bin/jobloop --help` produces verbatim:
  ```
  Traceback (most recent call last):
    File "/Users/mauriciohelfstein/dev/open-job-loop/.venv/bin/jobloop", line 3, in <module>
      from src.cli import app
  ModuleNotFoundError: No module named 'src.cli'
  ```
- **Boilerplate Regex Over-Truncation in `src/core/truncator.py`**:
  Lines 38-55 define `EEO_AND_BOILERPLATE_PATTERNS` using `(?is)...*?(?:\n\n|\Z)`.
  When executing `t.clean_boilerplate(text)` with text containing an EEO statement followed by single newline `\n` and job requirements:
  ```python
  text = """Company overview: We build AI tools. We are an equal opportunity employer.
  Key Requirements:
  - 5+ years of Python 3.12
  - Experience with DuckDB and SQLModel"""
  ```
  The returned output is verbatim: `'Company overview: We build AI tools.'`. All requirements after the EEO clause were swallowed and deleted.
  Similarly, for HTML without newlines between `<p>` tags:
  `<p>Hiring Senior Backend Engineer</p><p>We are an equal opportunity employer</p><p>Requirements: 5+ years with async Python</p>` produces verbatim: `'Hiring Senior Backend Engineer'`.
- **Pagination Drop in `src/db/repository.py`**:
  Lines 306-353 define `iterate_jobs(status, batch_size)`.
  When inserting 10 jobs with status `INGESTED` (`j0` through `j9`) and iterating with `batch_size=4` while calling `update_status(job.id, JobStatus.PREPROCESSED)` inside the consumer loop, the yielded job IDs are verbatim:
  `['j0', 'j1', 'j2', 'j3', 'j8', 'j9']`.
  Jobs `['j4', 'j5', 'j6', 'j7']` (40% of records) are completely skipped.
- **Double Truncation Idempotency in `src/core/truncator.py`**:
  Line 94 `_html_tags` regex `</?[a-zA-Z][a-zA-Z0-9:-]*(?:\s+[^>]*)?>` does not match `<job_posting>` due to underscore.
  Calling `t.truncate(t.truncate(text))` produces nested tags:
  ```xml
  <job_posting>
  <job_posting>
  ...
  &lt;/job_posting&gt;
  </job_posting>
  ```
- **Integrity Inspection**: Source files in `src/models/schemas.py`, `src/core/truncator.py`, `src/db/database.py`, `src/db/repository.py` contain zero hardcoded test outputs, zero dummy/mock facades in production modules, and genuine implementation logic.

## 2. Logic Chain

1. In `src/core/truncator.py`, using `(?is)` (DOTALL mode where `.` matches newlines) in conjunction with `.*?(?:\n\n|\Z)` causes non-greedy matching to match across single `\n` linebreaks if no `\n\n` exists in the text.
2. Job postings ingested from various web sources (HTML converted via single `\n` breaks or unformatted text) frequently contain single newlines separating paragraphs or lists.
3. Therefore, whenever any of the 9 boilerplate patterns appears in such text, `TextTruncator` strips not only the boilerplate sentence but also all subsequent sections of the job posting (qualifications, skills, compensation), leading to false-positive length validation errors (`DescriptionTooShortError`) or empty descriptions delivered to the triage LLM.
4. In `src/db/repository.py`, `iterate_jobs` executes SQL with `OFFSET ?`. When a consumer drains or transitions rows matching `status` to another status (standard DAG behavior across milestones), the index of remaining rows shifts backward while `offset` moves forward.
5. Consequently, advancing `offset += len(rows)` causes intermediate batches of jobs to be silently skipped and never processed.
6. The existence of these two high-blast-radius defects directly jeopardizes Milestone M2 (which consumes `TextTruncator` output for LLM triage) and Milestone M3 (which relies on `JobRepository.iterate_jobs` to drive the DAG pipeline).
7. Therefore, the required review verdict is **REQUEST_CHANGES**.

## 3. Caveats

- Unit test coverage in `tests/` currently passes 100% (41 of 41 tests) because the test cases only tested boilerplate placed at the end of the text, and only tested `iterate_jobs` without in-flight status transitions.
- Concurrency, WAL flush durability, SQL injection protection, and schema modeling are robust and fully functional.
- The requested changes do not require architectural restructuring; they only require fixing the regex bounds in `truncator.py`, adopting keyset pagination in `repository.py`, and adding a minimal CLI stub in `src/cli.py`.

## 4. Conclusion

**Verdict: REQUEST_CHANGES**

Milestone M1 cannot be approved in its current state due to two critical bugs:
1. `clean_boilerplate` regex greediness that swallows and deletes legitimate job posting content following boilerplate when separated by single newlines.
2. `iterate_jobs` offset pagination that silently drops records during state machine transitions.

Work is free of integrity violations and fundamentally sound. Upon resolving these findings, M1 will be ready for approval.

## 5. Verification Method

1. **Verify boilerplate single-newline handling**:
   Run:
   ```bash
   .venv/bin/python3 -c '
   from src.core.truncator import TextTruncator
   t = TextTruncator()
   text = """Overview: Acme AI.\nWe are an equal opportunity employer.\nRequirements:\n- Python 3.12\n- DuckDB"""
   cleaned = t.clean_boilerplate(text)
   assert "Python 3.12" in cleaned, f"Failed: Requirements deleted: {repr(cleaned)}"
   print("Boilerplate boundary test passed")
   '
   ```
2. **Verify in-flight status mutation during streaming iteration**:
   Run:
   ```bash
   .venv/bin/python3 -c '
   import asyncio
   from src.db.repository import JobRepository, compute_job_hash
   from src.models.schemas import JobPosting, JobStatus

   async def run():
       repo = JobRepository(":memory:")
       await repo.initialize()
       for i in range(10):
           h = compute_job_hash(f"d{i}", title=f"j{i}")
           await repo.save_job(JobPosting(id=f"j{i}", content_hash=h, title=f"j{i}", company="c", raw_description=f"d{i}", status=JobStatus.INGESTED))
       processed = []
       async for job in repo.iterate_jobs(status=JobStatus.INGESTED, batch_size=4):
           processed.append(job.id)
           await repo.update_status(job.id, JobStatus.PREPROCESSED)
       assert len(processed) == 10, f"Failed: only processed {len(processed)} of 10 items: {processed}"
       print("Streaming mutation test passed")
   asyncio.run(run())
   '
   ```
3. **Verify CLI entrypoint**:
   Run:
   ```bash
   .venv/bin/jobloop --help
   ```
   Must exit with code 0.
