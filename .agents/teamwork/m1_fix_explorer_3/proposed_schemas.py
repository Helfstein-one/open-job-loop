"""
Core domain models and Pydantic schemas for open-job-loop.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List, Any
from pydantic import BaseModel, Field, ConfigDict, model_validator, field_validator


class JobStatus(str, Enum):
    """
    Lifecycle status of a job posting throughout pipeline execution.
    """
    INGESTED = "INGESTED"
    DUPLICATE = "DUPLICATE"
    PREPROCESSED = "PREPROCESSED"
    TRIAGED = "TRIAGED"
    SHORTLISTED = "SHORTLISTED"
    DISCARDED = "DISCARDED"
    SKIPPED_TIMEOUT = "SKIPPED_TIMEOUT"
    ERROR = "ERROR"
    FAILED = "FAILED"  # Support for fatal pre-processing or unrecoverable failures


class Recommendation(str, Enum):
    """
    Triage recommendation decision.
    """
    SHORTLIST = "SHORTLIST"
    DISCARD = "DISCARD"


class MatchEvaluation(BaseModel):
    """
    Structured fit evaluation returned by the local LLM via Instructor.
    """
    model_config = ConfigDict(
        use_enum_values=False,
        populate_by_name=True,
        extra="ignore",
    )

    fit_score: int = Field(
        ...,
        ge=0,
        le=100,
        description="Fit score from 0 to 100 based on technical qualifications versus role requirements."
    )
    recommendation: Recommendation = Field(
        ...,
        description="Binary decision: SHORTLIST if candidate matches role threshold, else DISCARD."
    )
    matched_skills: Optional[List[str]] = Field(
        default_factory=list,
        description="List of required skills possessed by the candidate."
    )
    missing_skills: Optional[List[str]] = Field(
        default_factory=list,
        description="List of required skills absent from candidate profile."
    )
    reasoning: str = Field(
        default="",
        description="Concise rationale explaining the evaluation score."
    )
    seniority_fit: Optional[str] = Field(
        default=None,
        description="Assessment of seniority level match (e.g., 'Junior', 'Mid-level', 'Senior', 'Staff')."
    )

    @field_validator("matched_skills", "missing_skills", mode="before")
    @classmethod
    def coerce_none_skills(cls, v: Any) -> Any:
        """
        Coerces None or null LLM outputs for skill lists into empty lists.
        """
        if v is None:
            return []
        return v


class JobPosting(BaseModel):
    """
    Core entity model representing a job posting across ingestion, processing, and persistence.
    """
    model_config = ConfigDict(
        use_enum_values=False,
        populate_by_name=True,
        extra="ignore",
    )

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique identifier for the job posting record."
    )
    content_hash: str = Field(
        ...,
        description="Canonical SHA256 hex digest of normalized content used for deduplication."
    )
    title: str = Field(..., description="Job posting title.")
    company: str = Field(..., description="Company or organization hiring.")
    location: Optional[str] = Field(default=None, description="Location or remote designation.")
    raw_description: str = Field(..., description="Original raw description text as ingested.")
    cleaned_description: Optional[str] = Field(
        default=None,
        description="Cleaned, sanitized, and truncated description text."
    )
    description: Optional[str] = Field(
        default=None,
        description="Unified description text (synchronized with cleaned_description)."
    )
    url: Optional[str] = Field(default=None, description="Direct URL to posting if available.")
    status: JobStatus = Field(
        default=JobStatus.INGESTED,
        description="Current lifecycle status of the job posting."
    )
    fit_score: Optional[int] = Field(
        default=None,
        ge=0,
        le=100,
        description="Evaluated match score (0-100) if triaged."
    )
    recommendation: Optional[Recommendation] = Field(
        default=None,
        description="Triage recommendation (SHORTLIST or DISCARD)."
    )
    evaluation: Optional[MatchEvaluation] = Field(
        default=None,
        description="Full structured evaluation payload if triaged."
    )
    is_truncated: bool = Field(
        default=False,
        description="Flag indicating whether description was truncated to token threshold."
    )
    token_count: Optional[int] = Field(
        default=None,
        description="Estimated token count of processed description."
    )
    source: str = Field(
        default="mcp",
        description="Origin provider or MCP server name."
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Error details if job encountered error or failure."
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when record was created in UTC."
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when record was last updated in UTC."
    )

    @model_validator(mode="after")
    def sync_description_fields(self) -> JobPosting:
        """
        Synchronizes cleaned_description and description so either property can be read.
        Defaults description to raw_description if neither cleaned version is present.
        """
        if self.description is None and self.cleaned_description is not None:
            self.description = self.cleaned_description
        elif self.cleaned_description is None and self.description is not None:
            self.cleaned_description = self.description
        elif self.description is None and self.cleaned_description is None:
            self.description = self.raw_description
        return self


class CandidateProfile(BaseModel):
    """
    Profile representing candidate skills, preferences, and background used for LLM evaluation.
    """
    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
    )

    name: str = Field(..., description="Candidate full name.")
    target_role: str = Field(..., description="Target job title or specialization.")
    years_experience: int = Field(
        ...,
        ge=0,
        description="Total years of professional software engineering experience."
    )
    primary_skills: List[str] = Field(
        default_factory=list,
        description="Core technical competencies (languages, primary frameworks)."
    )
    secondary_skills: List[str] = Field(
        default_factory=list,
        description="Familiar tools, cloud platforms, and secondary libraries."
    )
    summary: str = Field(
        ...,
        description="Brief background summary, profile excerpt, or resume highlights."
    )

    def to_prompt_context(self) -> str:
        """
        Formats candidate profile into a structured context string for LLM system prompts.
        """
        primary = ", ".join(self.primary_skills) if self.primary_skills else "None specified"
        secondary = ", ".join(self.secondary_skills) if self.secondary_skills else "None specified"
        return (
            f"Candidate Name: {self.name}\n"
            f"Target Role: {self.target_role}\n"
            f"Years of Professional Experience: {self.years_experience}\n"
            f"Primary Technical Skills: {primary}\n"
            f"Secondary Skills & Tools: {secondary}\n"
            f"Professional Summary: {self.summary}"
        )
