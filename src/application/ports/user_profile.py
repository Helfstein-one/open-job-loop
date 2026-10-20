from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional


class UserProfilePort(abc.ABC):
    """
    Port interface for semantic profile storage, vector search, and Q&A retrieval.
    Backed by DuckDB VSS, Ollama embeddings, or mock implementations.
    """

    @abc.abstractmethod
    async def add_chunk(self, category: str, content: str) -> None:
        """Embed and persist a semantic profile chunk."""
        ...

    @abc.abstractmethod
    async def search_profile(self, query: str, limit: int = 3) -> List[Dict[str, Any]]:
        """Perform semantic cosine-similarity search over profile chunks."""
        ...

    @abc.abstractmethod
    async def synthesize_answer(
        self,
        question: str,
        job_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Synthesize candidate answer for a screening form field."""
        ...

    @abc.abstractmethod
    def list_chunks(self) -> List[Dict[str, Any]]:
        """List all stored profile chunks."""
        ...
