from __future__ import annotations

import abc
from typing import AsyncIterator, Dict, Optional

from src.domain.models import JobPosting, JobStatus


class JobRepositoryPort(abc.ABC):
    """
    Port interface for job postings persistence, retrieval, and deduplication.
    Follows Hexagonal Architecture (Driven / Secondary Port).
    """

    @abc.abstractmethod
    async def initialize(self) -> None:
        """Initialize database schema, tables, and indexes."""
        ...

    @abc.abstractmethod
    async def is_duplicate(self, content_hash: str) -> bool:
        """Check if a posting with identical SHA256 content hash exists."""
        ...

    @abc.abstractmethod
    async def save_job(self, job: JobPosting) -> bool:
        """Persist a new job posting entity into the store."""
        ...

    @abc.abstractmethod
    async def update_status(
        self,
        job_id: str,
        status: JobStatus,
        error_message: Optional[str] = None,
        increment_retry: bool = False,
    ) -> bool:
        """Update lifecycle status and error details for a job posting."""
        ...

    @abc.abstractmethod
    async def update_job(self, job: JobPosting) -> bool:
        """Update all mutable fields of an existing job posting."""
        ...

    @abc.abstractmethod
    async def get_job(self, job_id: str) -> Optional[JobPosting]:
        """Retrieve a single job posting by ID."""
        ...

    @abc.abstractmethod
    async def get_job_by_hash(self, content_hash: str) -> Optional[JobPosting]:
        """Retrieve a job posting by its unique content hash."""
        ...

    @abc.abstractmethod
    async def get_stats(self) -> Dict[str, int]:
        """Return aggregated counts grouped by JobStatus."""
        ...

    @abc.abstractmethod
    async def iterate_jobs(
        self,
        status: Optional[JobStatus] = None,
        batch_size: int = 100,
    ) -> AsyncIterator[JobPosting]:
        """Stream job postings matching an optional status filter."""
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        """Close connection and release underlying resources."""
        ...
