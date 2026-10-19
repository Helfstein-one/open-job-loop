import pytest
from unittest.mock import AsyncMock, MagicMock

from src.domain.models import ApplicationChannel
from src.infrastructure.adapters.ats.router import get_ats_adapter, detect_channel
from src.infrastructure.adapters.ats.greenhouse import GreenhouseAdapter
from src.infrastructure.adapters.ats.lever import LeverAdapter
from src.infrastructure.adapters.ats.ashby import AshbyAdapter
from src.application.use_cases.active_learning import ActiveLearningService


class TestATSAdaptersURLMatching:
    def test_greenhouse_matches_url(self):
        adapter = GreenhouseAdapter()
        assert adapter.matches_url("https://boards.greenhouse.io/stripe/jobs/12345")
        assert adapter.matches_url("https://job-boards.greenhouse.io/anthropic/jobs/6789")
        assert adapter.matches_url("https://grnh.se/abc123xyz")
        assert not adapter.matches_url("https://jobs.lever.co/openai/123")
        assert not adapter.matches_url("https://jobs.ashbyhq.com/figma/123")
        assert not adapter.matches_url("https://linkedin.com/jobs/view/123")

    def test_lever_matches_url(self):
        adapter = LeverAdapter()
        assert adapter.matches_url("https://jobs.lever.co/netflix/12345")
        assert adapter.matches_url("https://jobs.lever.co/spotify/6789/apply")
        assert not adapter.matches_url("https://boards.greenhouse.io/stripe/123")
        assert not adapter.matches_url("https://linkedin.com/jobs/view/123")

    def test_ashby_matches_url(self):
        adapter = AshbyAdapter()
        assert adapter.matches_url("https://jobs.ashbyhq.com/openai/12345")
        assert adapter.matches_url("https://jobs.ashbyhq.com/ramp/abc")
        assert not adapter.matches_url("https://boards.greenhouse.io/stripe/123")

    def test_router_get_adapter(self):
        gh = get_ats_adapter("https://boards.greenhouse.io/datadog/jobs/1")
        assert isinstance(gh, GreenhouseAdapter)

        lever = get_ats_adapter("https://jobs.lever.co/datadog/jobs/1")
        assert isinstance(lever, LeverAdapter)

        ashby = get_ats_adapter("https://jobs.ashbyhq.com/datadog/jobs/1")
        assert isinstance(ashby, AshbyAdapter)

        none_adapter = get_ats_adapter("https://example.com/careers/1")
        assert none_adapter is None

    def test_detect_channel(self):
        assert detect_channel("https://boards.greenhouse.io/co/1") == ApplicationChannel.GREENHOUSE
        assert detect_channel("https://jobs.lever.co/co/1") == ApplicationChannel.LEVER
        assert detect_channel("https://jobs.ashbyhq.com/co/1") == ApplicationChannel.ASHBY
        assert detect_channel("https://www.linkedin.com/jobs/view/123") == ApplicationChannel.LINKEDIN_EASY_APPLY
        assert detect_channel("https://unknown.com/job") == ApplicationChannel.EXTERNAL_MANUAL


class TestActiveLearningService:
    @pytest.mark.asyncio
    async def test_high_confidence_returns_directly_without_prompt(self):
        mock_pm = MagicMock()
        mock_pm.synthesize_answer_with_confidence = AsyncMock(return_value=("10 years", 0.95))

        service = ActiveLearningService(
            profile_manager=mock_pm,
            min_confidence=0.70,
            interactive=True,
        )

        ans = await service.get_or_learn_answer("Years of experience with Python?")
        assert ans == "10 years"
        mock_pm.add_chunk.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_confidence_prompts_and_persists_answer(self):
        mock_pm = MagicMock()
        mock_pm.synthesize_answer_with_confidence = AsyncMock(return_value=("Maybe 2 years", 0.40))
        mock_pm.add_chunk = AsyncMock()

        # Provide simulated candidate answer via input_fn
        def mock_candidate_input(q: str) -> str:
            return "8 years building distributed systems"

        service = ActiveLearningService(
            profile_manager=mock_pm,
            min_confidence=0.70,
            interactive=True,
            input_fn=mock_candidate_input,
        )

        ans = await service.get_or_learn_answer("How many years with Kubernetes clusters?")
        assert ans == "8 years building distributed systems"

        mock_pm.add_chunk.assert_called_once()
        args = mock_pm.add_chunk.call_args[1] if mock_pm.add_chunk.call_args[1] else mock_pm.add_chunk.call_args[0]
        if isinstance(args, tuple):
            category, content = args[0], args[1]
        else:
            category, content = args.get("category"), args.get("content")

        assert category == "ActiveLearning"
        assert "8 years building distributed systems" in content

    @pytest.mark.asyncio
    async def test_non_interactive_fallback(self):
        mock_pm = MagicMock()
        mock_pm.synthesize_answer_with_confidence = AsyncMock(return_value=("Best guess", 0.35))
        mock_pm.add_chunk = AsyncMock()

        service = ActiveLearningService(
            profile_manager=mock_pm,
            min_confidence=0.70,
            interactive=False,
        )

        ans = await service.get_or_learn_answer("Unmapped screening question?")
        assert ans == "Best guess"
        mock_pm.add_chunk.assert_not_called()
