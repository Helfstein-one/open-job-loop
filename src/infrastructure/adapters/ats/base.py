from __future__ import annotations
"""
Base classes and interfaces for ATS (Applicant Tracking System) form adapters.
"""

import abc
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from src.domain.models import ApplicationChannel
from src.application.use_cases.active_learning import ActiveLearningService
from src.application.ports.form_filler import ATSAdapterPort

logger = logging.getLogger(__name__)


class FormField(BaseModel):
    """
    Representation of an ATS form field discovered on the page.
    """
    name: str = Field(..., description="Field name or ID attribute.")
    label: str = Field(..., description="Visible label or question text.")
    field_type: str = Field(default="text", description="Input type (text, email, tel, file, select, radio, checkbox).")
    required: bool = Field(default=False, description="Whether the field is mandatory.")
    selector: str = Field(..., description="CSS selector to target the field.")
    options: List[str] = Field(default_factory=list, description="Dropdown or radio options if applicable.")


class ApplicationResult(BaseModel):
    """
    Structured outcome of an ATS form application execution.
    """
    success: bool = Field(..., description="Whether the application completed successfully.")
    channel: ApplicationChannel = Field(..., description="Platform through which application was executed.")
    job_id: Optional[str] = Field(default=None, description="Target job record ID.")
    fields_filled: int = Field(default=0, description="Total number of input fields populated.")
    logs: List[str] = Field(default_factory=list, description="Step-by-step application log messages.")
    dry_run: bool = Field(default=False, description="Whether the execution ran in dry-run mode.")
    error_message: Optional[str] = Field(default=None, description="Error message if execution failed.")


class BaseATSAdapter(ATSAdapterPort):
    """
    Abstract base class for platform-specific ATS adapters.
    """

    channel: ApplicationChannel

    def can_handle(self, url: str) -> bool:
        """Alias adhering to ATSAdapterPort contract."""
        return self.matches_url(url)

    @abc.abstractmethod
    def matches_url(self, url: str) -> bool:
        """
        Returns True if the given job URL belongs to this ATS platform.
        """
        pass

    @abc.abstractmethod
    async def fill_and_submit(
        self,
        page: Any,
        url: str,
        job_context: Dict[str, Any],
        active_learning: ActiveLearningService,
        resume_path: Optional[str] = None,
        dry_run: bool = False,
    ) -> ApplicationResult:
        """
        Navigates to the application form, inspects input fields, answers questions
        via Active Learning / DuckDB VSS, attaches the resume, and submits (or logs in dry-run).
        """
        pass
