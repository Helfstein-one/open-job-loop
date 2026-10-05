"""
Local LLM engine package: client factory, prompt formatting, and evaluator.
"""

from src.llm.client import (
    DEFAULT_API_KEY,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    create_llm_client,
    get_instructor_client,
)
from src.llm.evaluator import (
    JobFitEvaluator,
    LLMConnectionError,
    LLMError,
    LLMModelNotFoundError,
    LLMTimeoutError,
    LLMValidationError,
)
from src.llm.prompts import (
    DEFAULT_FIT_THRESHOLD,
    DEFAULT_SYSTEM_PROMPT,
    JOB_POSTING_TAG,
    build_evaluation_messages,
    build_evaluation_prompt,
    format_candidate_profile,
    strip_job_posting_tags,
    wrap_job_posting,
)

__all__ = [
    "DEFAULT_API_KEY",
    "DEFAULT_BASE_URL",
    "DEFAULT_FIT_THRESHOLD",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_SYSTEM_PROMPT",
    "DEFAULT_TIMEOUT",
    "JOB_POSTING_TAG",
    "JobFitEvaluator",
    "LLMConnectionError",
    "LLMError",
    "LLMModelNotFoundError",
    "LLMTimeoutError",
    "LLMValidationError",
    "build_evaluation_messages",
    "build_evaluation_prompt",
    "create_llm_client",
    "format_candidate_profile",
    "get_instructor_client",
    "strip_job_posting_tags",
    "wrap_job_posting",
]
