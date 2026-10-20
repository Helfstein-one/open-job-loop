from __future__ import annotations

import abc
from typing import Any, Dict, Optional

from src.domain.models import ApplicationChannel


class ATSAdapterPort(abc.ABC):
    """
    Port interface for multi-channel ATS and Easy Apply form automation.
    """

    @property
    @abc.abstractmethod
    def channel(self) -> ApplicationChannel:
        """Return the target channel this adapter handles."""
        ...

    @abc.abstractmethod
    def can_handle(self, url: str) -> bool:
        """Determine if this adapter supports the given target URL."""
        ...

    @abc.abstractmethod
    async def fill_and_submit(
        self,
        page: Any,
        url: str,
        job_context: Dict[str, Any],
        active_learning: Any,
        resume_path: Optional[str] = None,
        dry_run: bool = False,
    ) -> Any:
        """Fill candidate application and optionally submit in production."""
        ...
