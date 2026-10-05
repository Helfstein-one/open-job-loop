"""
Unit tests for domain models and schemas (src/models/schemas.py).
"""

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
    assert JobStatus.FAILED == "FAILED"


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
        reasoning="Strong candidate match.",
        seniority_fit="Senior"
    )
    assert eval_obj.fit_score == 85
    assert eval_obj.recommendation == Recommendation.SHORTLIST
    assert eval_obj.seniority_fit == "Senior"

    # Score boundary checks: 0 and 100
    eval_zero = MatchEvaluation(fit_score=0, recommendation=Recommendation.DISCARD)
    assert eval_zero.fit_score == 0
    eval_hundred = MatchEvaluation(fit_score=100, recommendation=Recommendation.SHORTLIST)
    assert eval_hundred.fit_score == 100

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


def test_match_evaluation_null_skills_coercion():
    # Direct instantiation with None
    eval_direct = MatchEvaluation(
        fit_score=90,
        recommendation=Recommendation.SHORTLIST,
        matched_skills=None,
        missing_skills=None,
    )
    assert eval_direct.matched_skills == []
    assert eval_direct.missing_skills == []

    # Dict validation with None
    eval_dict = MatchEvaluation.model_validate({
        "fit_score": 75,
        "recommendation": "DISCARD",
        "matched_skills": None,
        "missing_skills": None,
    })
    assert eval_dict.matched_skills == []
    assert eval_dict.missing_skills == []

    # JSON string validation with null
    eval_json = MatchEvaluation.model_validate_json(
        '{"fit_score": 80, "recommendation": "SHORTLIST", "matched_skills": null, "missing_skills": null}'
    )
    assert eval_json.matched_skills == []
    assert eval_json.missing_skills == []


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

    # Check description sync when description is provided
    job3 = JobPosting(
        content_hash="abc789hash",
        title="Staff Engineer",
        company="Gamma LLC",
        raw_description="Raw text",
        description="Explicit description"
    )
    assert job3.cleaned_description == "Explicit description"
    assert job3.description == "Explicit description"


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


def test_candidate_profile_empty_skills_context():
    profile = CandidateProfile(
        name="Jordan Lee",
        target_role="Junior Developer",
        years_experience=0,
        primary_skills=[],
        secondary_skills=[],
        summary="Recent graduate."
    )
    prompt_str = profile.to_prompt_context()
    assert "None specified" in prompt_str
