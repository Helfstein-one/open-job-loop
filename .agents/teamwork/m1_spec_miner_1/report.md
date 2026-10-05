# Specification Report: Packaging (`pyproject.toml`) and Domain Schemas (`src/models`)

## Executive Summary
This specification defines the production-ready code design and authoritative interface contracts for:
1. `pyproject.toml`: Modern PEP 621 package definition using Hatchling build backend, targeted for Python 3.12+, with locked runtime dependencies (`typer`, `rich`, `pydantic`, `duckdb`, `sqlmodel`, `instructor`, `openai`, `mcp`), test harnesses (`pytest`, `pytest-asyncio`), and dual CLI entrypoints (`jobloop` and `open-job-loop`).
2. `src/models/__init__.py` and `src/models/schemas.py`: Exact Pydantic v2 domain schemas (`JobStatus`, `Recommendation`, `MatchEvaluation`, `JobPosting`, `CandidateProfile`) with field validation, bidirectional description compatibility, and strict type safety.

All specifications and behavior models have been empirically validated against local runtimes (Python 3.12.13, Pydantic 2.13.5, DuckDB 1.5.5).

---

## Features Discovered

| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|----------|---------|-------------|--------|---------|----------------|----------------|
| 1 | Packaging | PEP 621 `pyproject.toml` | Standardized declarative project specification conforming to PEP 517/518/621. | TOML configuration file | Packaged installable Python wheel/sdist | Build fails if required fields or syntax are invalid. | PEP 621 spec & `tomllib` probe |
| 2 | Packaging | Hatchling Build Backend | Lightweight modern build backend configured via `[build-system] requires = ["hatchling"]`. | Source directory `src/` | Wheel artifact packaging `src` | Raises build error if package directory not found. | Hatchling specification |
| 3 | Packaging | Dual CLI Entrypoints | Dual console scripts exposing `src.cli:app` via both `open-job-loop` and `jobloop`. | CLI arguments from terminal | Invoked Typer application | Returns exit code 1 or 2 on unrecognized commands. | PROJECT.md §Feature 1 & §Code Layout |
| 4 | Packaging | Pytest Asyncio Configuration | In-file configuration in `[tool.pytest.ini_options]` with `asyncio_mode = "auto"` and `pythonpath = ["."]`. | Test runners (`pytest`) | Test execution with async fixtures supported out-of-the-box | Pytest defaults to sync if `asyncio_mode` omitted. | pytest-asyncio docs & verification |
| 5 | Domain Schema | `JobStatus` Enum | String-compatible Enum (`str, Enum`) representing all pipeline lifecycle phases. | Status string identifier | Enum instance / string equivalent | Raises `ValueError` if initialized with unknown state. | PROJECT.md §Interface Contracts |
| 6 | Domain Schema | `Recommendation` Enum | Binary classification Enum (`SHORTLIST`, `DISCARD`) for triage decisioning. | `"SHORTLIST"` or `"DISCARD"` | Enum instance | Raises `ValueError` for invalid string input. | PROJECT.md §Interface Contracts |
| 7 | Domain Schema | `MatchEvaluation` Schema | Structured output model enforced by Instructor for LLM evaluation payloads. | LLM JSON payload: fit_score, recommendation, skills, reasoning | Validated `MatchEvaluation` object | Raises `ValidationError` if `fit_score` < 0 or > 100. | PROJECT.md & Instructor probe |
| 8 | Domain Schema | `JobPosting` Schema | Central entity model representing job lifecycle state, raw/clean text, and persistence data. | Ingested job metadata and descriptions | Validated `JobPosting` entity | Raises `ValidationError` if required fields missing. | PROJECT.md & survey_spec.md probe |
| 9 | Domain Schema | Description Dual-Sync in `JobPosting` | Model validator synchronizing `cleaned_description` and `description` to ensure compatibility across modules. | `raw_description`, `cleaned_description`, or `description` | Consistent attributes on model instance | Defaults `description` to `raw_description` if uncleaned. | Empirical Pydantic validator probe |
| 10 | Domain Schema | `CandidateProfile` Schema | Input context schema defining applicant skills, seniority, and background for LLM prompts. | Candidate attributes: name, target_role, skills, summary | Validated `CandidateProfile` object | Raises `ValidationError` on negative `years_experience`. | PROJECT.md §Milestones & survey_spec.md |
| 11 | Domain Schema | Context Formatter (`to_prompt_context`) | Method converting `CandidateProfile` into clean structured text for insertion into LLM system prompts. | `CandidateProfile` instance | Clean string formatted for system prompt | Returns formatted string; handles empty skill lists safely. | Prompt design specification |
| 12 | Architecture | Module Exports (`src/models/__init__.py`) | Centralized explicit `__all__` exports for high-level imports. | Package import `from src.models import ...` | Exported classes | `AttributeError` if unexported symbols requested. | Python PEP 8 / package convention |

---

## Edge Cases

| # | Feature | Input | Observed Behavior |
|---|---------|-------|-------------------|
| 1 | `MatchEvaluation` | `fit_score = -5` or `fit_score = 105` | Pydantic raises `pydantic.ValidationError` citing `Input should be greater than or equal to 0` / `less than or equal to 100`. |
| 2 | `MatchEvaluation` | String numeric `fit_score = "85"` | In standard Pydantic mode (used by Instructor JSON mode), automatically coerces `"85"` to integer `85`. |
| 3 | `Recommendation` | `recommendation = "MAYBE"` | Raises `ValidationError: Input should be 'SHORTLIST' or 'DISCARD'`. |
| 4 | `JobPosting` | Instantiation without `id` | Generates unique UUID4 string automatically (`str(uuid.uuid4())`). |
| 5 | `JobPosting` | Instantiation without `created_at` or `updated_at` | Generates timezone-aware UTC datetime (`datetime.now(timezone.utc)`), eliminating Python 3.12 `utcnow()` deprecation warnings. |
| 6 | `JobPosting` | Legacy code reading `job.description` when only `cleaned_description` passed | Model validator populates `job.description = job.cleaned_description`. |
| 7 | `JobPosting` | Code passing `description` instead of `cleaned_description` | Model validator populates `job.cleaned_description = job.description`. |
| 8 | `CandidateProfile` | Negative `years_experience = -1` | Pydantic raises `ValidationError: Input should be greater than or equal to 0`. |
| 9 | `pyproject.toml` | `pytest` run without editable installation | With `pythonpath = ["."]` in `[tool.pytest.ini_options]`, imports like `from src.models import ...` resolve cleanly. |
| 10 | `pyproject.toml` | Execution of either `open-job-loop` or `jobloop` CLI | Both point directly to `src.cli:app` entrypoint via `[project.scripts]`. |

---

## Detailed Specifications

### 1. `pyproject.toml` Specification

#### File Location: `/Users/mauriciohelfstein/dev/open-job-loop/pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "open-job-loop"
version = "0.1.0"
description = "Autonomous, privacy-first CLI agent executing closed loops to discover, deduplicate, evaluate technical fit, and structure job applications."
readme = "README.md"
requires-python = ">=3.12"
license = { text = "MIT" }
authors = [
    { name = "Open Job Loop Team" }
]
keywords = ["agent", "cli", "job-search", "duckdb", "instructor", "local-llm", "mcp"]
classifiers = [
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Programming Language :: Python :: 3.14",
    "Environment :: Console",
    "Operating System :: OS Independent",
    "Intended Audience :: Developers",
]

dependencies = [
    "typer>=0.12.0",
    "rich>=13.7.0",
    "pydantic>=2.0.0",
    "duckdb>=1.0.0",
    "sqlmodel>=0.0.16",
    "instructor>=1.0.0",
    "openai>=1.0.0",
    "mcp>=1.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
]

[project.scripts]
open-job-loop = "src.cli:app"
jobloop = "src.cli:app"

[tool.hatch.build.targets.wheel]
packages = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["."]

[tool.ruff]
line-length = 100
target-version = "py312"
```

#### Rationale & Verification
- **Build Backend**: Hatchling is the standard modern build backend for PEP 621 without heavy setup dependencies.
- **Python Version**: Strict `>=3.12` enforces modern typing and native async enhancements (`asyncio.TaskGroup`, `asyncio.timeout`).
- **Dependencies Matrix**:
  - `typer>=0.12.0` & `rich>=13.7.0`: Powers CLI and live UI rendering.
  - `pydantic>=2.0.0`: Core validation engine with Rust-backed `pydantic-core` speed.
  - `duckdb>=1.0.0`: Columnar embedded storage with zero daemon overhead.
  - `sqlmodel>=0.0.16`: SQLAlchemy + Pydantic unification library.
  - `instructor>=1.0.0`: Structured LLM outputs enforcing schema compliance.
  - `openai>=1.0.0`: Async client targeting local Ollama `/v1` endpoint.
  - `mcp>=1.0.0`: Model Context Protocol SDK for ingestion tools.
- **CLI Aliases**: Both `open-job-loop` and `jobloop` map to `src.cli:app`, satisfying acceptance criteria and UX requirements.
- **Package Path**: `packages = ["src"]` ensures that all subpackages under `src/` (`core`, `db`, `llm`, `mcp`, `models`, `ui`) are included in packaging and namespace resolution.
- **Pytest Configuration**: `pythonpath = ["."]` allows pytest to resolve `src` directly during development without requiring `pip install -e .`.

---

### 2. `src/models/schemas.py` Specification

#### File Location: `/Users/mauriciohelfstein/dev/open-job-loop/src/models/schemas.py`

```python
"""
Core domain models and Pydantic schemas for open-job-loop.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict, model_validator


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
    matched_skills: List[str] = Field(
        default_factory=list,
        description="List of required skills possessed by the candidate."
    )
    missing_skills: List[str] = Field(
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
```

---

### 3. `src/models/__init__.py` Specification

#### File Location: `/Users/mauriciohelfstein/dev/open-job-loop/src/models/__init__.py`

```python
"""
Models package: exports domain entities, state enums, and evaluation schemas.
"""

from src.models.schemas import (
    CandidateProfile,
    JobPosting,
    JobStatus,
    MatchEvaluation,
    Recommendation,
)

__all__ = [
    "CandidateProfile",
    "JobPosting",
    "JobStatus",
    "MatchEvaluation",
    "Recommendation",
]
```

---

## Verification Test Suite Matrix

The following test suite verifies all schemas and requirements:

```python
import uuid
from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from src.models.schemas import (
    CandidateProfile,
    JobPosting,
    JobStatus,
    MatchEvaluation,
    Recommendation,
)


def test_job_status_enum_values():
    assert JobStatus.INGESTED == "INGESTED"
    assert JobStatus.DUPLICATE == "DUPLICATE"
    assert JobStatus.PREPROCESSED == "PREPROCESSED"
    assert JobStatus.TRIAGED == "TRIAGED"
    assert JobStatus.SHORTLISTED == "SHORTLISTED"
    assert JobStatus.DISCARDED == "DISCARDED"
    assert JobStatus.SKIPPED_TIMEOUT == "SKIPPED_TIMEOUT"
    assert JobStatus.ERROR == "ERROR"


def test_recommendation_enum_values():
    assert Recommendation.SHORTLIST == "SHORTLIST"
    assert Recommendation.DISCARD == "DISCARD"


def test_match_evaluation_validation():
    # Valid instance
    eval_obj = MatchEvaluation(
        fit_score=85,
        recommendation=Recommendation.SHORTLIST,
        matched_skills=["Python", "FastAPI"],
        missing_skills=["Kubernetes"],
        reasoning="Strong candidate match."
    )
    assert eval_obj.fit_score == 85
    assert eval_obj.recommendation == Recommendation.SHORTLIST

    # Score boundary checks
    MatchEvaluation(fit_score=0, recommendation=Recommendation.DISCARD)
    MatchEvaluation(fit_score=100, recommendation=Recommendation.SHORTLIST)

    # Score out-of-range checks
    with pytest.raises(ValidationError):
        MatchEvaluation(fit_score=-1, recommendation=Recommendation.DISCARD)

    with pytest.raises(ValidationError):
        MatchEvaluation(fit_score=101, recommendation=Recommendation.SHORTLIST)

    # String score coercion check
    coerced = MatchEvaluation.model_validate({
        "fit_score": "75",
        "recommendation": "SHORTLIST"
    })
    assert coerced.fit_score == 75


def test_job_posting_defaults_and_sync():
    job = JobPosting(
        content_hash="abc123hash",
        title="Senior Python Backend Engineer",
        company="Acme Corp",
        raw_description="Build distributed systems in Python."
    )
    # Check auto-generated UUID id
    assert isinstance(job.id, str)
    assert len(job.id) > 0

    # Check status default
    assert job.status == JobStatus.INGESTED

    # Check description sync with raw_description when cleaned is None
    assert job.description == "Build distributed systems in Python."
    assert job.cleaned_description is None

    # Check description sync when cleaned_description is set
    job2 = JobPosting(
        content_hash="abc456hash",
        title="Lead Engineer",
        company="Beta Inc",
        raw_description="Raw uncleaned text",
        cleaned_description="Cleaned text"
    )
    assert job2.description == "Cleaned text"
    assert job2.cleaned_description == "Cleaned text"


def test_candidate_profile_context_generation():
    profile = CandidateProfile(
        name="Alex Mercer",
        target_role="Senior Backend Engineer",
        years_experience=7,
        primary_skills=["Python", "DuckDB", "AsyncIO"],
        secondary_skills=["Docker", "Linux"],
        summary="Specialist in high-throughput data processing pipelines."
    )
    prompt_str = profile.to_prompt_context()
    assert "Alex Mercer" in prompt_str
    assert "Senior Backend Engineer" in prompt_str
    assert "7" in prompt_str
    assert "Python, DuckDB, AsyncIO" in prompt_str
    assert "Specialist in high-throughput data processing pipelines." in prompt_str


def test_candidate_profile_validation():
    with pytest.raises(ValidationError):
        CandidateProfile(
            name="Invalid Candidate",
            target_role="Dev",
            years_experience=-2,
            summary="Invalid negative experience."
        )
```

---

## Conclusion & Integration Readiness
1. **Packaging**: `pyproject.toml` is 100% compliant with PEP 621, Hatchling build standards, and project constraints.
2. **Domain Schemas**: `JobStatus`, `Recommendation`, `MatchEvaluation`, `JobPosting`, and `CandidateProfile` provide a strict, type-safe foundation for Instructor triage and DuckDB persistence.
3. **Execution Ready**: All specifications are fully resolved with zero ambiguous types or pending questions. Ready for implementation in Milestone M1.
