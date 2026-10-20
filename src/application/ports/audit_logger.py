from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional


class AuditLoggerPort(abc.ABC):
    """
    Port interface for auditing submitted applications and recording execution logs.
    """

    @abc.abstractmethod
    def log_application(
        self,
        job_id: str,
        job_title: str,
        company: str,
        channel: str,
        status: str,
        fields_submitted: List[Dict[str, Any]],
        logs: str,
    ) -> None:
        """Persist an audit trail record for an application attempt."""
        ...

    @abc.abstractmethod
    def log_execution(
        self,
        level: str,
        module: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist a system operational log event."""
        ...
