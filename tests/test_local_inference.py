"""
Unit and integration tests asserting fit_score thresholding against local Llama 3.2
and offline mock verification using fixtures/golden_jobs.json.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.llm.evaluator import JobFitEvaluator
from src.llm.prompts import build_evaluation_messages
from src.mcp.mock_client import MockMcpJobClient
from src.models.schemas import (
    CandidateProfile,
    JobPosting,
    MatchEvaluation,
    Recommendation,
)

FIXTURE_PATH = Path("fixtures/golden_jobs.json")


def is_ollama_model_available(
    base_url: str = "http://localhost:11434",
    model_name: str = "llama3.2:3b",
) -> bool:
    """
    Check if the local Ollama daemon is reachable and has the specified model available.
    """
    try:
        req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode("utf-8"))
            models = data.get("models", [])
            names = [m.get("name", "") for m in models]
            return any(model_name in name or "llama3.2" in name for name in names)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return False


@pytest.fixture
def golden_jobs() -> list[dict[str, Any]]:
    """Load golden job fixtures from disk."""
    assert FIXTURE_PATH.exists(), f"Fixture file not found: {FIXTURE_PATH}"
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, list)
    assert len(data) == 6, f"Expected 6 golden jobs, got {len(data)}"
    return data


@pytest.fixture
def golden_candidate_profile() -> CandidateProfile:
    """Target candidate profile for golden evaluation."""
    return CandidateProfile(
        name="Senior Python / AI Systems Engineer",
        target_role="Senior Python / AI Systems Engineer",
        years_experience=7,
        primary_skills=[
            "Python 3.12",
            "AsyncIO",
            "local open-weight models (Llama 3.2, Ollama)",
            "Instructor",
            "OpenAI SDK",
            "DuckDB",
            "SQLModel",
            "Model Context Protocol (MCP)",
            "Pydantic",
            "Typer",
            "Rich",
            "CLI utilities",
            "unit testing",
            "execution harnesses",
        ],
        secondary_skills=[
            "Docker",
            "Linux",
            "PostgreSQL",
            "FastAPI",
        ],
        summary=(
            "Senior Python and AI Systems Engineer with 7+ years of experience building "
            "autonomous closed-loop agent systems, Model Context Protocol (MCP) servers and clients, "
            "local LLM orchestration (Llama 3.2, Ollama) with Instructor, DuckDB data pipelines, "
            "CLI tooling with Typer and Rich, and execution harnesses with timeout guards."
        ),
    )


# ==============================================================================
# Offline Mock & Schema Verification Tests (Run reliably in CI without Ollama)
# ==============================================================================

class TestGoldenFixturesOffline:
    """Verify structure, partitioning, and schema compliance of golden_jobs.json."""

    def test_golden_jobs_structure_and_partition(self, golden_jobs: list[dict[str, Any]]):
        matches = [j for j in golden_jobs if j.get("expected_match") is True]
        mismatches = [j for j in golden_jobs if j.get("expected_match") is False]

        assert len(matches) == 3, f"Expected 3 matches, got {len(matches)}"
        assert len(mismatches) == 3, f"Expected 3 mismatches, got {len(mismatches)}"

        # Validate match job fields
        for job in matches:
            assert job["expected_decision"] == "shortlist"
            assert job["min_expected_fit_score"] >= 0.70
            assert "description" in job and len(job["description"]) > 50
            assert "title" in job and len(job["title"]) > 0
            assert "company" in job and len(job["company"]) > 0

        # Validate mismatch job fields
        for job in mismatches:
            assert job["expected_decision"] == "discard"
            assert job["max_expected_fit_score"] <= 0.40
            assert "description" in job and len(job["description"]) > 50
            assert "title" in job and len(job["title"]) > 0
            assert "company" in job and len(job["company"]) > 0

    def test_mock_mcp_client_loads_golden_fixtures(self):
        client = MockMcpJobClient(fixture_path=FIXTURE_PATH)
        assert client.total_jobs == 6
        assert len(client._jobs) == 6
        for j in client._jobs:
            assert isinstance(j, JobPosting)
            assert j.title
            assert j.company
            assert j.raw_description

    def test_prompt_structure_contains_xml_and_candidate_profile(
        self,
        golden_jobs: list[dict[str, Any]],
        golden_candidate_profile: CandidateProfile,
    ):
        for job in golden_jobs:
            messages = build_evaluation_messages(
                job_description=job["description"],
                candidate_profile=golden_candidate_profile,
            )
            assert len(messages) == 2
            assert messages[0]["role"] == "system"
            assert messages[1]["role"] == "user"
            assert "<job_posting>" in messages[1]["content"]
            assert "</job_posting>" in messages[1]["content"]
            assert "Python 3.12" in messages[1]["content"]

    @pytest.mark.asyncio
    async def test_offline_thresholding_logic_with_mock_evaluator(
        self,
        golden_jobs: list[dict[str, Any]],
        golden_candidate_profile: CandidateProfile,
    ):
        """Simulate evaluator thresholding with mock client to guarantee logic consistency."""
        mock_client = MagicMock()
        evaluator = JobFitEvaluator(client=mock_client, score_threshold=70)

        for job in golden_jobs:
            if job["expected_match"]:
                mock_eval = MatchEvaluation(
                    fit_score=85,
                    recommendation=Recommendation.SHORTLIST,
                    matched_skills=["Python", "AsyncIO", "MCP"],
                    missing_skills=[],
                    reasoning="Strong technical alignment.",
                )
            else:
                mock_eval = MatchEvaluation(
                    fit_score=15,
                    recommendation=Recommendation.DISCARD,
                    matched_skills=[],
                    missing_skills=["Python", "AsyncIO", "MCP"],
                    reasoning="Unrelated domain.",
                )

            mock_client.chat.completions.create = AsyncMock(return_value=mock_eval)
            result = await evaluator.evaluate_fit(
                job_description=job["description"],
                candidate_profile=golden_candidate_profile,
            )

            if job["expected_match"]:
                assert result.fit_score >= 70
                assert result.recommendation == Recommendation.SHORTLIST
            else:
                assert result.fit_score < 70
                assert result.recommendation == Recommendation.DISCARD


# ==============================================================================
# Live Local Llama 3.2 Integration Tests (Requires running Ollama with llama3.2)
# ==============================================================================

@pytest.mark.skipif(
    not is_ollama_model_available(),
    reason="Local Ollama server (http://localhost:11434) or model llama3.2:3b not available",
)
class TestLiveLocalInference:
    """Live inference tests asserting fit_score thresholding against local Llama 3.2."""

    @pytest.fixture(autouse=True)
    def setup_evaluator(self):
        self.evaluator = JobFitEvaluator(
            base_url="http://localhost:11434/v1",
            model="llama3.2:3b",
            timeout=60.0,
            score_threshold=70,
        )
        yield
        # teardown is handled automatically

    @pytest.mark.asyncio
    async def test_live_golden_matches_shortlisted(
        self,
        golden_jobs: list[dict[str, Any]],
        golden_candidate_profile: CandidateProfile,
    ):
        """Verify all 3 matching golden jobs evaluate to fit_score >= 70 and SHORTLIST."""
        matches = [j for j in golden_jobs if j.get("expected_match") is True]
        assert len(matches) == 3

        for job in matches:
            result = await self.evaluator.evaluate_fit(
                job_description=job["description"],
                candidate_profile=golden_candidate_profile,
            )

            assert isinstance(result, MatchEvaluation)
            assert result.fit_score >= 70, (
                f"Job {job['id']} ('{job['title']}') failed match threshold: "
                f"expected >= 70, got {result.fit_score}. Reasoning: {result.reasoning}"
            )
            assert result.recommendation == Recommendation.SHORTLIST, (
                f"Job {job['id']} recommendation expected SHORTLIST, got {result.recommendation}"
            )
            assert len(result.reasoning) > 0
            assert isinstance(result.matched_skills, list)
            assert len(result.matched_skills) > 0

    @pytest.mark.asyncio
    async def test_live_golden_mismatches_discarded(
        self,
        golden_jobs: list[dict[str, Any]],
        golden_candidate_profile: CandidateProfile,
    ):
        """Verify all 3 mismatching golden jobs evaluate to fit_score < 70 and DISCARD."""
        mismatches = [j for j in golden_jobs if j.get("expected_match") is False]
        assert len(mismatches) == 3

        for job in mismatches:
            result = await self.evaluator.evaluate_fit(
                job_description=job["description"],
                candidate_profile=golden_candidate_profile,
            )

            assert isinstance(result, MatchEvaluation)
            assert result.fit_score < 70, (
                f"Job {job['id']} ('{job['title']}') failed mismatch threshold: "
                f"expected < 70, got {result.fit_score}. Reasoning: {result.reasoning}"
            )
            assert result.recommendation == Recommendation.DISCARD, (
                f"Job {job['id']} recommendation expected DISCARD, got {result.recommendation}"
            )
            assert len(result.reasoning) > 0

    @pytest.mark.asyncio
    async def test_live_all_six_golden_jobs_contract(
        self,
        golden_jobs: list[dict[str, Any]],
        golden_candidate_profile: CandidateProfile,
    ):
        """Comprehensive verification of all 6 golden jobs against acceptance criteria."""
        results = []
        for job in golden_jobs:
            eval_res = await self.evaluator.evaluate_fit(
                job_description=job["description"],
                candidate_profile=golden_candidate_profile,
            )
            results.append((job, eval_res))

        for job, eval_res in results:
            if job["expected_match"]:
                assert eval_res.fit_score >= 70
                assert eval_res.recommendation == Recommendation.SHORTLIST
            else:
                assert eval_res.fit_score < 70
                assert eval_res.recommendation == Recommendation.DISCARD
