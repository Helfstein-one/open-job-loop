"""
Core JobFitEvaluator engine powered by local Instructor and Ollama.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import instructor
from instructor.core import InstructorRetryException
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError
from pydantic import ValidationError

from src.llm.client import (
    DEFAULT_API_KEY,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    get_instructor_client,
)
from src.llm.prompts import (
    DEFAULT_FIT_THRESHOLD,
    build_evaluation_messages,
    format_candidate_profile,
)
from src.models.schemas import CandidateProfile, MatchEvaluation, Recommendation

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Base exception for LLM operations."""


class LLMConnectionError(LLMError):
    """Raised when connection to local LLM endpoint fails (Ollama offline)."""


class LLMTimeoutError(LLMError, TimeoutError):
    """
    Raised when LLM evaluation exceeds configured timeout limit.
    Inherits from Python's built-in TimeoutError for direct harness compatibility.
    """


class LLMValidationError(LLMError):
    """Raised when the LLM output cannot be parsed into a valid MatchEvaluation schema."""


class LLMModelNotFoundError(LLMError):
    """Raised when the requested model is not found in the Ollama instance (404)."""


class JobFitEvaluator:
    """
    Evaluates job postings against candidate profiles using local open-weight LLMs via Instructor.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = "llama3.2:3b",
        api_key: str = DEFAULT_API_KEY,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        temperature: float = 0.0,
        score_threshold: int = DEFAULT_FIT_THRESHOLD,
        system_prompt: str | None = None,
        client: instructor.AsyncInstructor | None = None,
        enforce_threshold_consistency: bool = True,
    ) -> None:
        """
        Initialize JobFitEvaluator.

        :param base_url: Ollama API endpoint URL (default 'http://localhost:11434/v1').
        :param model: Target model name (default 'llama3.2:3b').
        :param api_key: API key string (default 'ollama').
        :param timeout: Wall-clock timeout in seconds per evaluation (default 30.0).
        :param max_retries: Transient retry count (default 2).
        :param temperature: Sampling temperature for deterministic scoring (default 0.0).
        :param score_threshold: Minimum fit score for SHORTLIST recommendation (default 70).
        :param system_prompt: Optional override for system prompt instructions.
        :param client: Optional pre-configured instructor.AsyncInstructor instance for dependency injection.
        :param enforce_threshold_consistency: Automatically align recommendation with score_threshold.
        """
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature
        self.score_threshold = score_threshold
        self.system_prompt = system_prompt
        self.enforce_threshold_consistency = enforce_threshold_consistency

        self._custom_client = client is not None
        self.client: instructor.AsyncInstructor = client or get_instructor_client(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

    async def evaluate_fit(
        self,
        job_description: str,
        candidate_profile: str | CandidateProfile | dict[str, Any],
    ) -> MatchEvaluation:
        """
        Evaluate candidate fit against job description and return structured MatchEvaluation.

        :param job_description: Text content of target job posting.
        :param candidate_profile: Candidate qualifications as string, CandidateProfile, or dict.
        :return: MatchEvaluation instance.
        :raises ValueError: If job_description or candidate_profile is empty.
        :raises LLMTimeoutError: If evaluation exceeds timeout limit.
        :raises LLMConnectionError: If LLM service cannot be reached.
        :raises LLMModelNotFoundError: If target model is missing in Ollama.
        :raises LLMValidationError: If response violates MatchEvaluation schema.
        :raises LLMError: On other unhandled LLM API errors.
        """
        if not job_description or not job_description.strip():
            raise ValueError("job_description cannot be empty or whitespace-only")

        formatted_profile = format_candidate_profile(candidate_profile)
        if not formatted_profile or not formatted_profile.strip():
            raise ValueError("candidate_profile cannot be empty or whitespace-only")

        messages = build_evaluation_messages(
            job_description=job_description,
            candidate_profile=candidate_profile,
            system_prompt=self.system_prompt,
        )

        try:
            async with asyncio.timeout(self.timeout):
                evaluation: MatchEvaluation = await self.client.chat.completions.create(
                    model=self.model,
                    response_model=MatchEvaluation,
                    messages=messages,
                    temperature=self.temperature,
                    max_retries=self.max_retries,
                )
        except (TimeoutError, APITimeoutError) as exc:
            logger.error("LLM evaluation timed out after %.1f seconds: %s", self.timeout, exc)
            raise LLMTimeoutError(f"LLM evaluation timed out after {self.timeout}s: {exc}") from exc
        except APIConnectionError as exc:
            logger.error("Failed to connect to LLM endpoint at %s: %s", self.base_url, exc)
            raise LLMConnectionError(
                f"Failed to connect to LLM at {self.base_url}. Ensure Ollama is running: {exc}"
            ) from exc
        except APIStatusError as exc:
            if exc.status_code == 404:
                logger.error("Model '%s' not found on Ollama instance: %s", self.model, exc)
                raise LLMModelNotFoundError(
                    f"Model '{self.model}' not found at {self.base_url}: {exc}"
                ) from exc
            logger.error("LLM API status error (%s): %s", exc.status_code, exc)
            raise LLMError(f"LLM API status error ({exc.status_code}): {exc}") from exc
        except (InstructorRetryException, ValidationError) as exc:
            logger.error("LLM output validation error: %s", exc)
            raise LLMValidationError(f"LLM response failed MatchEvaluation validation: {exc}") from exc
        except Exception as exc:
            if isinstance(exc, (LLMError, TimeoutError, ValueError)):
                raise
            logger.error("Unexpected LLM evaluation failure: %s", exc)
            raise LLMError(f"Unexpected LLM evaluation failure: {exc}") from exc

        # Guarantee recommendation-threshold alignment and contradiction defense if enabled
        if self.enforce_threshold_consistency:
            matched = evaluation.matched_skills or []
            missing = evaluation.missing_skills or []
            if len(matched) == 0 and len(missing) > 0 and evaluation.fit_score >= 70:
                logger.warning(
                    "Contradiction detected: fit_score=%d with 0 matched skills and %d missing skills. Clamping fit_score to 0 and recommendation to DISCARD.",
                    evaluation.fit_score,
                    len(missing),
                )
                evaluation = evaluation.model_copy(
                    update={
                        "fit_score": 0,
                        "recommendation": Recommendation.DISCARD,
                    }
                )
            elif evaluation.fit_score >= self.score_threshold and evaluation.recommendation != Recommendation.SHORTLIST:
                evaluation = evaluation.model_copy(update={"recommendation": Recommendation.SHORTLIST})
            elif evaluation.fit_score < self.score_threshold and evaluation.recommendation != Recommendation.DISCARD:
                evaluation = evaluation.model_copy(update={"recommendation": Recommendation.DISCARD})

        return evaluation

    async def health_check(self) -> bool:
        """
        Probe endpoint availability and model accessibility.
        Returns True if reachable and responsive, False otherwise.
        """
        try:
            raw_client = getattr(self.client, "client", None)
            if raw_client and hasattr(raw_client, "models"):
                async with asyncio.timeout(5.0):
                    await raw_client.models.list()
                    return True
            return True
        except (OpenAIError, TimeoutError, OSError) as exc:
            logger.warning("JobFitEvaluator health check failed: %s", exc)
            return False

    async def close(self) -> None:
        """
        Close underlying AsyncOpenAI client connection pool.
        """
        if not self._custom_client:
            raw_client = getattr(self.client, "client", None)
            if raw_client and hasattr(raw_client, "close"):
                await raw_client.close()
