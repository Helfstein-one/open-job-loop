# Milestone M1 Fix Explorer 2 Handoff Report

**Agent**: M1 Fix Explorer 2 (`teamwork_preview_explorer`)  
**Type**: Hard Handoff  
**Scope**: `src/db/repository.py` fixes (Keyset pagination, `Tuple` import, `compute_job_hash` hardening)

---

## 1. Observation

1. **In-Flight Mutation Row Skipping (`src/db/repository.py:306-353`)**:
   - `JobRepository.iterate_jobs` executes `SELECT * FROM job_postings WHERE status = ? ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?;` with `offset += len(rows)`.
   - When consumers mutate record status in-flight (e.g. from `JobStatus.INGESTED` to `JobStatus.PREPROCESSED`), records shift out of the filtered set.
   - Empirical reproduction over 20 seeded records with `batch_size=5`: exactly 10 records were processed and 10 records were skipped (50% data loss).
2. **Missing `Tuple` Import (`src/db/repository.py:13, 365`)**:
   - Line 13: `from typing import Any, AsyncIterator, Dict, List, Optional` (omits `Tuple`).
   - Line 365: `def _row_to_job(self, cols: List[str], row: Tuple[Any, ...]) -> JobPosting:` uses `Tuple`.
   - Runtime execution of `typing.get_type_hints(JobRepository._row_to_job)` crashes with `NameError: name 'Tuple' is not defined` (reproduced in `tests/test_adversarial_m1.py:309`).
3. **Delimiter Collision in `compute_job_hash` (`src/db/repository.py:19-46`)**:
   - Line 44 uses `"::".join(parts)` where omitted optional fields (`None`) are omitted from `parts`.
   - `compute_job_hash("Desc", title=None, company="Acme")` and `compute_job_hash("Desc", title="Acme", company=None)` both evaluated to payload `"acme::desc"`, generating identical SHA256 hashes (`assert h_title_none == h_company_none` reproduced in `tests/test_stress_persistence.py:357-362`).
   - In addition, fields containing literal `"::"` caused inter-field delimiter collisions.

---

## 2. Logic Chain

1. **Keyset Pagination Solution**:
   - Monotonic cursor tracking on `(created_at, id)` guarantees O(1) memory and immune to row deletion or status mutation.
   - DuckDB composite row constructor `(created_at, id) > (?, ?)` raises `BinderException: Cannot compare values of type STRUCT(TIMESTAMP, VARCHAR) and type STRUCT(VARCHAR, VARCHAR)` when parameters are bound without explicit casts.
   - The expanded comparison `(created_at > ? OR (created_at = ? AND id > ?))` executes cleanly and leverages existing index `idx_jobs_created_at`.
   - Empirical test of 500 records with in-flight status mutation processed 500/500 items (0 skipped, 0 duplicates) with 137.96 KB peak memory (well below the 350 KB O(1) threshold).
2. **`Tuple` Import Solution**:
   - Adding `Tuple` to line 13 typing imports resolves all type annotations, enabling runtime reflection without altering runtime execution.
3. **Canonical Hashing Solution**:
   - Using `json.dumps({"c": norm_company, "d": norm_desc, "t": norm_title}, sort_keys=True, separators=(",", ":"))` guarantees delimiter escaping and strict structural disambiguation.
   - `compute_job_hash("Desc", title=None, company="Acme")` yields `7b78f59d...`, while `compute_job_hash("Desc", title="Acme", company=None)` yields `ae8d02e3...`. Collisions are completely eliminated at ~5 microseconds per hash.

---

## 3. Caveats

- Updating `compute_job_hash` changes SHA256 outputs for records with `title` or `company`. For new databases, this is seamless; for any pre-existing DuckDB file from prior runs, previously stored `content_hash` values would differ from newly calculated hashes.
- `tests/test_stress_persistence.py:362` and `tests/test_adversarial_m1.py:309` originally contained `pytest.raises` or equality assertions documenting the bugs. When applying these fixes, those test assertions must be updated to assert the corrected behavior (provided in `test_fixes.patch`).

---

## 4. Conclusion

All three issues in `src/db/repository.py` have concrete, validated fixes specified and packaged into machine-applicable patch files:
- Patch file: `.agents/teamwork/m1_fix_explorer_2/src_db_repository.patch`
- Test updates patch: `.agents/teamwork/m1_fix_explorer_2/test_fixes.patch`
- Comprehensive report: `.agents/teamwork/m1_fix_explorer_2/report.md`

All fixes maintain strict O(1) RAM bounds, zero data loss during streaming iteration, clean typing reflection, and robust SHA256 content deduplication.

---

## 5. Verification Method

1. **Verify Keyset Pagination and Status Mutation**:
   ```bash
   .venv/bin/python -c "
   import asyncio
   from datetime import datetime
   from src.db.repository import JobRepository
   from src.models.schemas import JobPosting, JobStatus

   async def check():
       repo = JobRepository(':memory:')
       await repo.initialize()
       for i in range(20):
           await repo.save_job(JobPosting(id=f'j{i:02d}', content_hash=f'h{i:02d}', title='T', company='C', raw_description=f'Desc {i}'))
       count = 0
       async for j in repo.iterate_jobs(status=JobStatus.INGESTED, batch_size=4):
           count += 1
           await repo.update_status(j.id, JobStatus.PREPROCESSED)
       assert count == 20
       await repo.close()
   asyncio.run(check())
   "
   ```
2. **Verify Type Hints**:
   ```bash
   .venv/bin/python -c "
   import typing
   from src.db.repository import JobRepository
   hints = typing.get_type_hints(JobRepository._row_to_job)
   assert 'row' in hints
   print('Type hints OK:', hints)
   "
   ```
3. **Verify Collision Resistance**:
   ```bash
   .venv/bin/python -c "
   from src.db.repository import compute_job_hash
   h1 = compute_job_hash('Desc', title=None, company='Acme')
   h2 = compute_job_hash('Desc', title='Acme', company=None)
   assert h1 != h2
   print('Collision resistance OK')
   "
   ```
4. **Run Pytest Suites**:
   ```bash
   .venv/bin/pytest tests/test_db.py tests/test_stress_persistence.py -v
   ```
