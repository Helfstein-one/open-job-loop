from __future__ import annotations
"""
Domain Job Repository for open-job-loop.

Implements immediate disk persistence, atomic SHA256 content deduplication,
DAG state transitions, and bounded O(1) RAM streaming queries.
"""


from datetime import datetime, timezone
import hashlib
import json
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from src.infrastructure.adapters.database import DatabaseManager
from src.domain.models import JobPosting, JobStatus, MatchEvaluation, Recommendation


def compute_job_hash(
    raw_description: str,
    title: Optional[str] = None,
    company: Optional[str] = None
) -> str:
    """
    Compute a canonical SHA256 hex digest for job posting content deduplication.
    Normalizes whitespace and casing for robust deduplication.

    Args:
        raw_description: Original job description text.
        title: Optional job title.
        company: Optional hiring organization.

    Returns:
        64-character lowercase SHA256 hex digest.
    """
    parts = []
    if company:
        parts.append(company.strip().lower())
    if title:
        parts.append(title.strip().lower())
    normalized_desc = " ".join(raw_description.strip().split())
    parts.append(normalized_desc)

    payload = "::".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class JobRepository:
    """
    Repository providing persistence, deduplication, and lifecycle updates
    for JobPosting domain entities backed by DuckDB.
    """

    def __init__(
        self,
        db_path: str = "open_job_loop.duckdb",
        db_manager: Optional[DatabaseManager] = None
    ) -> None:
        """
        Initialize the repository.

        Args:
            db_path: Path to the DuckDB file or ':memory:'.
            db_manager: Optional pre-configured DatabaseManager instance.
        """
        self.db_path = db_path
        self._db = db_manager or DatabaseManager(db_path=db_path)

    async def initialize(self) -> None:
        """Initialize database schema and indexes."""
        await self._db.ainitialize()

    async def is_duplicate(self, content_hash: str) -> bool:
        """
        Check if a job posting with the specified SHA256 content hash exists in the database.

        Args:
            content_hash: SHA256 hex digest string.

        Returns:
            True if a matching record exists, False otherwise.
        """
        query = "SELECT 1 FROM job_postings WHERE content_hash = ? LIMIT 1;"
        res = await self._db.aexecute_scalar(query, (content_hash,))
        return res is not None

    async def save_job(self, job: JobPosting) -> bool:
        """
        Persist a new job posting immediately to DuckDB.
        Uses ON CONFLICT (content_hash) DO NOTHING for atomic deduplication.

        Args:
            job: JobPosting entity to persist.

        Returns:
            True if the job was newly inserted.
            False if insertion was skipped due to a duplicate content_hash.
        """
        query = """
        INSERT INTO job_postings (
            id, content_hash, title, company, location,
            raw_description, cleaned_description, status,
            fit_score, recommendation, evaluation, is_truncated,
            token_count, url, source, error_message,
            created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?
        )
        ON CONFLICT (content_hash) DO NOTHING
        RETURNING id;
        """

        # Serialize evaluation if present
        eval_json: Optional[str] = None
        if job.evaluation is not None:
            if hasattr(job.evaluation, "model_dump_json"):
                eval_json = job.evaluation.model_dump_json()
            elif hasattr(job.evaluation, "json"):
                eval_json = job.evaluation.json()
            else:
                eval_json = json.dumps(job.evaluation)

        # Normalize timestamps
        now_utc = datetime.now(timezone.utc)
        created_at = job.created_at or now_utc
        updated_at = job.updated_at or now_utc

        # Handle Recommendation enum or string
        rec_val = job.recommendation.value if isinstance(job.recommendation, Recommendation) else job.recommendation

        # Handle JobStatus enum or string
        status_val = job.status.value if isinstance(job.status, JobStatus) else str(job.status)

        params = (
            job.id,
            job.content_hash,
            job.title,
            job.company,
            job.location,
            job.raw_description,
            job.cleaned_description,
            status_val,
            job.fit_score,
            rec_val,
            eval_json,
            job.is_truncated,
            job.token_count,
            job.url,
            job.source,
            job.error_message,
            created_at,
            updated_at,
        )

        rows = await self._db.aexecute_write(query, params, checkpoint=True)
        return len(rows) > 0

    async def update_status(
        self,
        job_id: str,
        status: JobStatus,
        fit_score: Optional[int] = None,
        recommendation: Optional[Recommendation] = None,
        error_message: Optional[str] = None
    ) -> bool:
        """
        Update a job's lifecycle status, fit score, and recommendation with immediate flush.

        Args:
            job_id: Unique identifier of the job record.
            status: Target JobStatus enum.
            fit_score: Optional score (0-100).
            recommendation: Optional Recommendation enum (SHORTLIST or DISCARD).
            error_message: Optional error details.

        Returns:
            True if the job record was found and updated, False otherwise.
        """
        status_val = status.value if isinstance(status, JobStatus) else str(status)
        rec_val = recommendation.value if isinstance(recommendation, Recommendation) else recommendation
        now_utc = datetime.now(timezone.utc)

        query = """
        UPDATE job_postings
        SET status = ?,
            fit_score = COALESCE(?, fit_score),
            recommendation = COALESCE(?, recommendation),
            error_message = COALESCE(?, error_message),
            updated_at = ?
        WHERE id = ?
        RETURNING id;
        """
        params = (status_val, fit_score, rec_val, error_message, now_utc, job_id)
        rows = await self._db.aexecute_write(query, params, checkpoint=True)
        return len(rows) > 0

    async def update_job(self, job: JobPosting) -> bool:
        """
        Update all mutable fields of an existing job posting (e.g. after pre-processing or triage).

        Args:
            job: Updated JobPosting entity.

        Returns:
            True if updated, False if record did not exist.
        """
        eval_json: Optional[str] = None
        if job.evaluation is not None:
            if hasattr(job.evaluation, "model_dump_json"):
                eval_json = job.evaluation.model_dump_json()
            elif hasattr(job.evaluation, "json"):
                eval_json = job.evaluation.json()
            else:
                eval_json = json.dumps(job.evaluation)

        now_utc = datetime.now(timezone.utc)
        rec_val = job.recommendation.value if isinstance(job.recommendation, Recommendation) else job.recommendation
        status_val = job.status.value if isinstance(job.status, JobStatus) else str(job.status)

        query = """
        UPDATE job_postings
        SET cleaned_description = ?,
            status = ?,
            fit_score = ?,
            recommendation = ?,
            evaluation = ?,
            is_truncated = ?,
            token_count = ?,
            error_message = ?,
            updated_at = ?
        WHERE id = ?
        RETURNING id;
        """
        params = (
            job.cleaned_description,
            status_val,
            job.fit_score,
            rec_val,
            eval_json,
            job.is_truncated,
            job.token_count,
            job.error_message,
            now_utc,
            job.id,
        )
        rows = await self._db.aexecute_write(query, params, checkpoint=True)
        return len(rows) > 0

    async def get_job(self, job_id: str) -> Optional[JobPosting]:
        """
        Retrieve a single job posting by ID. Guarantees O(1) memory usage.

        Args:
            job_id: Unique record ID.

        Returns:
            JobPosting instance or None if not found.
        """
        query = "SELECT * FROM job_postings WHERE id = ? LIMIT 1;"
        cols, rows = await self._db.aexecute_read(query, (job_id,))
        if not rows:
            return None
        return self._row_to_job(cols, rows[0])

    async def get_job_by_hash(self, content_hash: str) -> Optional[JobPosting]:
        """
        Retrieve a single job posting by content hash. Guarantees O(1) memory usage.

        Args:
            content_hash: SHA256 digest string.

        Returns:
            JobPosting instance or None if not found.
        """
        query = "SELECT * FROM job_postings WHERE content_hash = ? LIMIT 1;"
        cols, rows = await self._db.aexecute_read(query, (content_hash,))
        if not rows:
            return None
        return self._row_to_job(cols, rows[0])

    async def get_stats(self) -> Dict[str, int]:
        """
        Aggregate count of job postings grouped by status.

        Returns:
            Dictionary containing counts for all JobStatus values and 'total'.
        """
        query = "SELECT status, COUNT(*) FROM job_postings GROUP BY status;"
        cols, rows = await self._db.aexecute_read(query)

        # Initialize dictionary with zeros for all standard statuses
        stats: Dict[str, int] = {s.value: 0 for s in JobStatus}
        total = 0

        for status_val, count in rows:
            stats[str(status_val)] = int(count)
            total += int(count)

        stats["total"] = total
        return stats

    async def iterate_jobs(
        self,
        status: Optional[JobStatus] = None,
        batch_size: int = 50
    ) -> AsyncIterator[JobPosting]:
        """
        Stream job postings from DuckDB with bounded memory consumption.
        Guarantees O(1) RAM footprint by paging fixed chunks using keyset pagination
        on (created_at, id). Mutating job statuses in-flight during iteration
        will never skip rows or re-read previously yielded rows.

        Args:
            status: Optional filter by JobStatus.
            batch_size: Number of records to load per pagination chunk.

        Yields:
            JobPosting entities one by one.
        """
        if batch_size < 1:
            batch_size = 50

        last_created_at: Optional[Any] = None
        last_id: Optional[str] = None

        while True:
            params: List[Any] = []
            where_clauses: List[str] = []

            if status is not None:
                status_val = status.value if isinstance(status, JobStatus) else str(status)
                where_clauses.append("status = ?")
                params.append(status_val)

            if last_created_at is not None and last_id is not None:
                where_clauses.append("(created_at > ? OR (created_at = ? AND id > ?))")
                params.extend([last_created_at, last_created_at, last_id])

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            query = f"""
            SELECT * FROM job_postings
            {where_sql}
            ORDER BY created_at ASC, id ASC
            LIMIT ?;
            """
            params.append(batch_size)

            cols, rows = await self._db.aexecute_read(query, params)
            if not rows:
                break

            ts_idx = cols.index("created_at")
            id_idx = cols.index("id")
            last_created_at = rows[-1][ts_idx]
            last_id = rows[-1][id_idx]

            for row in rows:
                yield self._row_to_job(cols, row)

            if len(rows) < batch_size:
                break

    async def close(self) -> None:
        """Close database connection."""
        await self._db.aclose()

    async def __aenter__(self) -> JobRepository:
        await self.initialize()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    def _row_to_job(self, cols: List[str], row: Tuple[Any, ...]) -> JobPosting:
        """
        Map a database row tuple to a validated JobPosting Pydantic model.
        """
        data = dict(zip(cols, row))

        # Parse JSON evaluation if present
        eval_raw = data.get("evaluation")
        if eval_raw:
            if isinstance(eval_raw, str):
                try:
                    eval_data = json.loads(eval_raw)
                    data["evaluation"] = MatchEvaluation(**eval_data)
                except Exception:
                    data["evaluation"] = None
            elif isinstance(eval_raw, dict):
                data["evaluation"] = MatchEvaluation(**eval_raw)

        # Parse Recommendation enum
        rec_raw = data.get("recommendation")
        if rec_raw and isinstance(rec_raw, str):
            try:
                data["recommendation"] = Recommendation(rec_raw)
            except ValueError:
                data["recommendation"] = None

        # Parse JobStatus enum
        status_raw = data.get("status")
        if status_raw and isinstance(status_raw, str):
            try:
                data["status"] = JobStatus(status_raw)
            except ValueError:
                data["status"] = JobStatus.INGESTED

        # Filter strictly to fields recognized by JobPosting schema
        valid_fields = {k: v for k, v in data.items() if k in JobPosting.model_fields}
        return JobPosting(**valid_fields)
