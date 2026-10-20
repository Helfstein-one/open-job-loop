from pathlib import Path

import pytest

from src.application.use_cases.onboard import ProfilerAgent


@pytest.fixture
def agent():
    return ProfilerAgent(mock_mode=True)


@pytest.mark.asyncio
async def test_profiler_agent_analyze_mock(agent):
    analysis = await agent.analyze_profile("Sample raw text of LinkedIn profile")
    assert "candidate" in analysis
    assert "target_profile" in analysis

    target = analysis["target_profile"]
    assert len(target["target_roles"]) > 0
    assert "Staff" in target["seniority_level"] or "Senior" in target["seniority_level"]
    assert len(target["suggested_questions"]) >= 3


def test_profiler_agent_compile_yaml(agent, tmp_path):
    out_yaml = str(tmp_path / "compiled_profile.yaml")
    analysis = {
        "candidate": {
            "name": "Mauricio Helfstein",
            "title": "Staff Data Engineer",
            "years_of_experience": 10,
            "location": "São Paulo, Brazil",
            "summary": "Experienced Data Engineer",
        },
        "target_profile": {
            "target_roles": ["Staff Data Engineer", "Principal Engineer"],
            "seniority_level": "Staff",
            "suggested_keywords": "Staff Data Engineer Python",
            "estimated_salary_range": "$140k - $160k",
            "key_strengths": ["Python", "Spark", "DuckDB"],
            "suggested_questions": [],
        },
    }
    answers = {
        "salary": "$150,000 USD / year",
        "work_model": "100% Remoto",
        "visa": "Brasileiro; requer sponsorship",
    }
    compiled = agent.compile_profile_yaml(analysis, answers, output_path=out_yaml)
    assert Path(out_yaml).exists()
    assert compiled["candidate"]["name"] == "Mauricio Helfstein"
    assert compiled["target_job_profile"]["salary_target"] == "$150,000 USD / year"
    assert compiled["work_authorization"]["requires_sponsorship_us"] is True


def test_profiler_agent_extract_pdf_missing(agent):
    with pytest.raises(FileNotFoundError):
        agent.extract_from_pdf("non_existent_file.pdf")
