"""
Tier 3: Cross-Feature & Pairwise Integration Tests.

Validates the full 5-stage DAG pipeline:
Stage 1: MCP Ingestion (MockMcpJobClient with fixtures/golden_jobs.json)
Stage 2: Deduplication (DuckDB SHA256 content hash)
Stage 3: Pre-Processing (TextTruncator boilerplate stripping & XML wrapping)
Stage 4: Triage (JobFitEvaluator via Instructor)
Stage 5: Decision Tree & Persistence (SHORTLISTED vs DISCARDED flushed immediately to DuckDB)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.harness import LocalLoopGuard
from src.core.pipeline import JobPipeline, PipelineConfig
from src.core.truncator import TextTruncator
from src.db.repository import JobRepository
from src.llm.evaluator import JobFitEvaluator
from src.mcp.mock_client import MockMcpJobClient
from src.models.schemas import CandidateProfile, MatchEvaluation, Recommendation
from tests.test_local_inference import is_ollama_model_available

FIXTURE_PATH = Path("fixtures/golden_jobs.json")


@pytest.fixture
def target_candidate() -> CandidateProfile:
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
        secondary_skills=["Docker", "Linux", "PostgreSQL", "FastAPI"],
        summary=(
            "Senior Python and AI Systems Engineer with 7+ years of experience building "
            "autonomous closed-loop agent systems, Model Context Protocol (MCP) servers and clients, "
            "local LLM orchestration (Llama 3.2, Ollama) with Instructor, DuckDB data pipelines, "
            "CLI tooling with Typer and Rich, and execution harnesses with timeout guards."
        ),
    )


class TestTier3PipelineIntegrationOffline:
    """Offline 5-stage pipeline execution with deterministic evaluator mock."""

    @pytest.mark.asyncio
    async def test_full_pipeline_cross_feature_offline(self, tmp_path: Path, target_candidate: CandidateProfile):
        db_path = str(tmp_path / "tier3_offline.duckdb")
        repo = JobRepository(db_path=db_path)
        await repo.initialize()

        # Mock instructor client simulating appropriate scores for matches/mismatches
        mock_client = MagicMock()

        async def _mock_triage(model, response_model, messages, **kwargs):
            # Extract user message containing job description
            user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
            if "Nurse" in user_msg or "ICU" in user_msg or "TikTok" in user_msg or "Java" in user_msg:
                return MatchEvaluation(
                    fit_score=10,
                    recommendation=Recommendation.DISCARD,
                    matched_skills=[],
                    missing_skills=["Python", "AsyncIO"],
                    reasoning="Unrelated domain.",
                )
            return MatchEvaluation(
                fit_score=85,
                recommendation=Recommendation.SHORTLIST,
                matched_skills=["Python", "AsyncIO", "DuckDB", "MCP"],
                missing_skills=[],
                reasoning="Strong technical alignment.",
            )

        mock_client.chat.completions.create = AsyncMock(side_effect=_mock_triage)

        evaluator = JobFitEvaluator(client=mock_client, score_threshold=70)
        mcp_client = MockMcpJobClient(fixture_path=FIXTURE_PATH)
        await mcp_client.connect()
        truncator = TextTruncator(max_tokens=1500)
        guard = LocalLoopGuard(max_iterations=10, timeout_seconds=15.0)

        pipeline = JobPipeline(
            repository=repo,
            evaluator=evaluator,
            ingestion_client=mcp_client,
            candidate_profile=target_candidate,
            truncator=truncator,
            guard=guard,
            config=PipelineConfig(score_threshold=70, batch_size=10),
        )

        # Run pipeline
        result = await pipeline.run()

        # Ingestion metrics
        assert result.total_ingested == 6
        assert result.duplicates == 0
        assert result.shortlisted == 3
        assert result.discarded == 3
        assert result.errors == 0
        assert result.skipped_timeout == 0

        # Verify DuckDB persistence
        stats = await repo.get_stats()
        assert stats["total"] == 6
        assert stats.get("SHORTLISTED", 0) == 3
        assert stats.get("DISCARDED", 0) == 3

        # Verify second run deduplication
        mcp_client_second = MockMcpJobClient(fixture_path=FIXTURE_PATH)
        await mcp_client_second.connect()
        pipeline_second = JobPipeline(
            repository=repo,
            evaluator=evaluator,
            ingestion_client=mcp_client_second,
            candidate_profile=target_candidate,
            truncator=truncator,
            guard=guard,
            config=PipelineConfig(score_threshold=70, batch_size=10),
        )
        result_second = await pipeline_second.run()

        # All 6 jobs should be identified as duplicates
        assert result_second.total_ingested == 6
        assert result_second.duplicates == 6
        assert result_second.shortlisted == 0
        assert result_second.discarded == 0

        await repo.close()


@pytest.mark.skipif(
    not is_ollama_model_available(),
    reason="Local Ollama daemon or model llama3.2:3b not available",
)
class TestTier3PipelineIntegrationLive:
    """Live 5-stage pipeline execution using local Ollama llama3.2:3b."""

    @pytest.mark.asyncio
    async def test_full_pipeline_live_golden_evaluation(
        self,
        tmp_path: Path,
        target_candidate: CandidateProfile,
    ):
        db_path = str(tmp_path / "tier3_live.duckdb")
        repo = JobRepository(db_path=db_path)
        await repo.initialize()

        evaluator = JobFitEvaluator(
            base_url="http://localhost:11434/v1",
            model="llama3.2:3b",
            timeout=60.0,
            score_threshold=70,
        )
        mcp_client = MockMcpJobClient(fixture_path=FIXTURE_PATH)
        await mcp_client.connect()
        truncator = TextTruncator(max_tokens=1500)
        guard = LocalLoopGuard(max_iterations=10, timeout_seconds=60.0)

        pipeline = JobPipeline(
            repository=repo,
            evaluator=evaluator,
            ingestion_client=mcp_client,
            candidate_profile=target_candidate,
            truncator=truncator,
            guard=guard,
            config=PipelineConfig(score_threshold=70, batch_size=10),
        )

        result = await pipeline.run()

        assert result.total_ingested == 6
        assert result.shortlisted == 3
        assert result.discarded == 3
        assert result.errors == 0
        assert result.skipped_timeout == 0

        # Verify DuckDB records
        stats = await repo.get_stats()
        assert stats["total"] == 6
        assert stats.get("SHORTLISTED", 0) == 3
        assert stats.get("DISCARDED", 0) == 3

        await repo.close()
