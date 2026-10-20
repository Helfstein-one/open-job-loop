from __future__ import annotations

import abc

from src.domain.models import CandidateProfile, JobPosting, MatchEvaluation


class JobEvaluatorPort(abc.ABC):
    """
    Port interface for evaluating candidate fit against a job posting.
    Supports single-stage LLM evaluation and multi-tier cascade evaluation.
    """

    @abc.abstractmethod
    async def evaluate_fit(
        self,
        job: JobPosting,
        profile: CandidateProfile,
    ) -> MatchEvaluation:
        """
        Evaluate candidate fit against job description and return structured evaluation.
        """
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        """Release underlying HTTP sessions or model handles."""
        ...
