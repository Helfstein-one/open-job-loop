import pytest
import os
from pathlib import Path
from src.application.use_cases.profile import ProfileManager, compute_mock_embedding


SAMPLE_PROFILE_YAML = """
candidate:
  name: "Mauricio Helfstein"
  email: "mauricio@test.com"
  phone: "+55 11 99999-9999"
  location: "São Paulo, Brazil"
  title: "Staff Data Engineer"
  years_of_experience: 10
  linkedin_url: "https://linkedin.com/in/mauricio"

work_authorization:
  authorized_in_brazil: true
  authorized_in_us: false
  requires_sponsorship_us: true

preferences:
  expected_salary_usd: "$140,000"
  notice_period: "2 weeks"

summary: "Staff Data Engineer with 10+ years experience in Python, DuckDB, and Spark."

skills:
  languages: ["Python", "SQL"]
  data_engineering: ["DuckDB", "Spark"]

standard_answers:
  - question: "How many years of Python experience do you have?"
    answer: "10"
  - question: "What is your expected salary?"
    answer: "$140,000"
  - question: "Do you require visa sponsorship?"
    answer: "Yes"
"""


@pytest.fixture
def temp_profile_yaml(tmp_path):
    p = tmp_path / "test_profile.yaml"
    p.write_text(SAMPLE_PROFILE_YAML)
    return str(p)


@pytest.fixture
def temp_db_path(tmp_path):
    return str(tmp_path / "test_vss.duckdb")


def test_compute_mock_embedding_dimensions_and_normalization():
    vec = compute_mock_embedding("Python Staff Engineer 10 years experience", dim=768)
    assert len(vec) == 768
    norm = sum(x * x for x in vec)
    assert pytest.approx(norm, rel=1e-3) == 1.0


def test_chunk_profile_categories(temp_profile_yaml):
    manager = ProfileManager(mock_mode=True)
    import yaml
    with open(temp_profile_yaml) as f:
        data = yaml.safe_load(f)
    chunks = manager.chunk_profile(data)
    categories = [c[0] for c in chunks]
    assert "personal_bio" in categories
    assert "professional_summary" in categories
    assert "work_authorization" in categories
    assert "compensation_preferences" in categories
    assert "technical_skills" in categories
    assert "standard_qa" in categories


@pytest.mark.asyncio
async def test_sync_and_vss_search(temp_profile_yaml, temp_db_path):
    manager = ProfileManager(db_path=temp_db_path, mock_mode=True)
    count = await manager.sync_from_yaml(temp_profile_yaml)
    assert count >= 6

    # Test query about Python experience
    results = await manager.search_profile("How many years of Python experience?", limit=2)
    assert len(results) > 0
    top_cat = results[0]["category"]
    assert top_cat in ["standard_qa", "technical_skills", "personal_bio"]


@pytest.mark.asyncio
async def test_synthesize_answer_pattern_identification(temp_profile_yaml, temp_db_path):
    manager = ProfileManager(db_path=temp_db_path, mock_mode=True)
    await manager.sync_from_yaml(temp_profile_yaml)

    # Question about Python
    ans_py = await manager.synthesize_answer("How many years of Python experience do you have?")
    assert "10" in ans_py

    # Question about salary
    ans_sal = await manager.synthesize_answer("What is your expected salary?")
    assert "$140,000" in ans_sal

    # Question about sponsorship
    ans_spon = await manager.synthesize_answer("Do you require visa sponsorship?")
    assert "Yes" in ans_spon


def test_list_chunks_empty_and_populated(temp_profile_yaml, temp_db_path):
    manager = ProfileManager(db_path=temp_db_path, mock_mode=True)
    manager.initialize_schema()
    assert manager.list_chunks() == []
