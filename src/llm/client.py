"""
Local LLM client factory wrapping AsyncOpenAI with Instructor in JSON mode.
"""

from __future__ import annotations

import os

import instructor
from openai import AsyncOpenAI

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_API_KEY = "ollama"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2


def resolve_base_url(base_url: str | None = None) -> str:
    """
    Resolve base URL prioritizing explicit parameter, then environment variables,
    defaulting to local Ollama API.
    """
    if base_url:
        return base_url.strip()
    return os.getenv("OLLAMA_BASE_URL", os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL)).strip()


def resolve_api_key(api_key: str | None = None) -> str:
    """
    Resolve API key prioritizing explicit parameter, then environment variables,
    defaulting to 'ollama'.
    """
    if api_key:
        return api_key.strip()
    return os.getenv("OLLAMA_API_KEY", os.getenv("OPENAI_API_KEY", DEFAULT_API_KEY)).strip()


def get_instructor_client(
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> instructor.AsyncInstructor:
    """
    Create and return an AsyncInstructor client configured for local open-weight inference.

    :param base_url: Target base URL (defaults to http://localhost:11434/v1).
    :param api_key: Authentication key (defaults to 'ollama').
    :param timeout: Request timeout in seconds (default 30.0).
    :param max_retries: Network connection retries on transient errors (default 2).
    :return: An AsyncInstructor instance operating in instructor.Mode.JSON.
    """
    resolved_url = resolve_base_url(base_url)
    resolved_key = resolve_api_key(api_key)

    raw_client = AsyncOpenAI(
        base_url=resolved_url,
        api_key=resolved_key,
        timeout=timeout,
        max_retries=max_retries,
    )

    return instructor.from_openai(raw_client, mode=instructor.Mode.JSON)


# Alias for flexible factory import
create_llm_client = get_instructor_client
