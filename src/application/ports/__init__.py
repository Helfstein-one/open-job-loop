from __future__ import annotations

from src.application.ports.audit_logger import AuditLoggerPort
from src.application.ports.form_filler import ATSAdapterPort
from src.application.ports.job_evaluator import JobEvaluatorPort
from src.application.ports.job_ingestion import JobIngestionPort
from src.application.ports.job_repository import JobRepositoryPort
from src.application.ports.user_profile import UserProfilePort

# Backward compatibility alias
JobRepository = JobRepositoryPort
LLMClient = JobEvaluatorPort
JobSearch = JobIngestionPort

__all__ = [
    "JobRepositoryPort",
    "JobEvaluatorPort",
    "JobIngestionPort",
    "UserProfilePort",
    "AuditLoggerPort",
    "ATSAdapterPort",
    "JobRepository",
    "LLMClient",
    "JobSearch",
]
