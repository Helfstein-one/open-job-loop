from __future__ import annotations

import abc
from typing import Any, List

from src.domain.models import JobPosting


class JobIngestionPort(abc.ABC):
    """
    Port interface for job ingestion clients (LinkedIn MCP, Mock, REST APIs).
    Driven/Secondary Port in Hexagonal Architecture.
    """

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish connection with the job provider."""
        ...

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Terminate connection and release resources."""
        ...

    @abc.abstractmethod
    async def fetch_jobs(self, limit: int = 10, **kwargs: Any) -> List[JobPosting]:
        """Fetch up to `limit` raw postings and normalize into JobPosting entities."""
        ...
