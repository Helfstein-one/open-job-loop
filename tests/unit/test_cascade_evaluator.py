"""
Unit tests for CascadeJobEvaluator (3-Tier Cascade Evaluation).
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.domain.models import CandidateProfile, MatchEvaluation, Recommendation
from src.infrastructure.adapters.cascade_evaluator import (
    CascadeJobEvaluator,
    DEFAULT_FAST_MODEL,
    DEFAULT_DEEP_MODEL,
    DEEP_HIGH_PRECISION_MODEL,
)


@pytest.fixture
def sample_profile() -> CandidateProfile:
    return CandidateProfile(
        name="Mauricio Helfstein",
        target_role="Staff Data Engineer",
        years_experience=10,
        primary_skills=["Python", "DuckDB", "Spark", "SQL", "Kafka"],
        secondary_skills=["AWS", "Docker", "Kubernetes"],
        summary="Staff Data Engineer with 10+ years designing distributed systems.",
    )


@pytest.mark.asyncio
async def test_cascade_evaluator_init_defaults():
    evaluator = CascadeJobEvaluator()
    assert evaluator.model_fast == DEFAULT_FAST_MODEL
    assert evaluator.model_deep == DEFAULT_DEEP_MODEL
    assert evaluator.model == DEFAULT_DEEP_MODEL
    assert evaluator.vss_min_similarity == 0.35
    assert evaluator.enable_vss_prefilter is True
    assert evaluator.enable_fast_gatekeeper is True


@pytest.mark.asyncio
async def test_cascade_tier0_vss_discard(sample_profile):
    """When VSS similarity is below threshold, Tier 0 discards immediately."""
    evaluator = CascadeJobEvaluator(
        vss_min_similarity=0.40,
        enable_vss_prefilter=True,
        enable_fast_gatekeeper=True,
        db_path="data/test.duckdb",
        mock_mode=True,
    )

    with patch.object(evaluator, "_check_vss_similarity", new_callable=AsyncMock) as mock_vss:
        mock_vss.return_value = 0.22  # Low similarity

        with patch.object(evaluator, "_run_fast_gatekeeper", new_callable=AsyncMock) as mock_gatekeeper:
            eval = await evaluator.evaluate_fit("Senior Marketing Manager job posting...", sample_profile)

            assert eval.recommendation == Recommendation.DISCARD
            assert "[Tier 0 VSS]" in eval.reasoning
            mock_gatekeeper.assert_not_called()


@pytest.mark.asyncio
async def test_cascade_tier1_gatekeeper_discard(sample_profile):
    """When Tier 0 passes but Tier 1 Gatekeeper says NO, discards before Tier 2."""
    evaluator = CascadeJobEvaluator(
        enable_vss_prefilter=False,
        enable_fast_gatekeeper=True,
    )

    with patch.object(evaluator, "_run_fast_gatekeeper", new_callable=AsyncMock) as mock_gatekeeper:
        mock_gatekeeper.return_value = (False, "Sales role completely out of scope")

        with patch.object(CascadeJobEvaluator, "evaluate_fit", wraps=evaluator.evaluate_fit):
            # Patch parent super().evaluate_fit to assert it's never called
            with patch("src.infrastructure.adapters.llm_evaluator.JobFitEvaluator.evaluate_fit", new_callable=AsyncMock) as mock_deep:
                eval = await evaluator.evaluate_fit("Enterprise Sales Executive...", sample_profile)

                assert eval.recommendation == Recommendation.DISCARD
                assert "[Tier 1 Gatekeeper" in eval.reasoning
                assert "Sales role completely out of scope" in eval.reasoning
                mock_deep.assert_not_called()


@pytest.mark.asyncio
async def test_cascade_tier2_deep_evaluation(sample_profile):
    """When Tier 1 passes (YES), proceeds to Tier 2 Deep Technical Evaluation."""
    evaluator = CascadeJobEvaluator(
        enable_vss_prefilter=False,
        enable_fast_gatekeeper=True,
    )

    expected_eval = MatchEvaluation(
        fit_score=92,
        recommendation=Recommendation.SHORTLIST,
        reasoning="Exceptional fit for Staff Data Engineer role.",
        matched_skills=["Python", "DuckDB", "Spark"],
        missing_skills=[],
    )

    with patch.object(evaluator, "_run_fast_gatekeeper", new_callable=AsyncMock) as mock_gatekeeper:
        mock_gatekeeper.return_value = (True, "Plausible technical fit for Staff Data role")

        with patch("src.infrastructure.adapters.llm_evaluator.JobFitEvaluator.evaluate_fit", new_callable=AsyncMock) as mock_deep:
            mock_deep.return_value = expected_eval

            result = await evaluator.evaluate_fit("Staff Data Engineer at Nubank...", sample_profile)

            assert result.fit_score == 92
            assert result.recommendation == Recommendation.SHORTLIST
            mock_deep.assert_called_once()


@pytest.mark.asyncio
async def test_cascade_gatekeeper_parsing_yes_and_no():
    """Verify parsing logic of gatekeeper LLM responses."""
    evaluator = CascadeJobEvaluator()

    mock_resp_no = MagicMock()
    mock_resp_no.choices = [MagicMock(message=MagicMock(content="NO: Marketing position"))]

    mock_resp_yes = MagicMock()
    mock_resp_yes.choices = [MagicMock(message=MagicMock(content="YES: Data engineering match"))]

    with patch.object(evaluator.fast_raw_client.chat.completions, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_resp_no
        passed, reason = await evaluator._run_fast_gatekeeper("Marketing job", "Profile")
        assert passed is False
        assert "Marketing position" in reason

        mock_create.return_value = mock_resp_yes
        passed, reason = await evaluator._run_fast_gatekeeper("Data job", "Profile")
        assert passed is True
        assert "Data engineering match" in reason


@pytest.mark.asyncio
async def test_cascade_gatekeeper_fail_open_on_error():
    """Verify fail-open resilience: gatekeeper passes if LLM times out or errors."""
    evaluator = CascadeJobEvaluator()

    with patch.object(evaluator.fast_raw_client.chat.completions, "create", side_effect=TimeoutError("Request timed out")):
        passed, reason = await evaluator._run_fast_gatekeeper("Some job", "Profile")
        assert passed is True
        assert "Gatekeeper bypassed" in reason
