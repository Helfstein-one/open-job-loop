from __future__ import annotations

"""
ATS Adapter Router and Channel Detection.
"""

from typing import List, Optional
from urllib.parse import urlparse

from src.domain.models import ApplicationChannel
from src.infrastructure.adapters.ats.ashby import AshbyAdapter
from src.infrastructure.adapters.ats.base import BaseATSAdapter
from src.infrastructure.adapters.ats.greenhouse import GreenhouseAdapter
from src.infrastructure.adapters.ats.lever import LeverAdapter

_ADAPTERS: List[BaseATSAdapter] = [
    GreenhouseAdapter(),
    LeverAdapter(),
    AshbyAdapter(),
]


def get_ats_adapter(url: str) -> Optional[BaseATSAdapter]:
    """
    Returns the appropriate ATS adapter for the given URL, or None if external/unsupported.
    """
    if not url:
        return None
    for adapter in _ADAPTERS:
        if adapter.matches_url(url):
            return adapter
    return None


def detect_channel(url: str) -> ApplicationChannel:
    """
    Infers the ApplicationChannel from a URL.
    """
    if not url:
        return ApplicationChannel.EXTERNAL_MANUAL

    netloc = urlparse(url).netloc.lower()
    if "greenhouse.io" in netloc or "grnh.se" in netloc:
        return ApplicationChannel.GREENHOUSE
    if "lever.co" in netloc:
        return ApplicationChannel.LEVER
    if "ashbyhq.com" in netloc:
        return ApplicationChannel.ASHBY
    if "linkedin.com" in netloc:
        return ApplicationChannel.LINKEDIN_EASY_APPLY

    return ApplicationChannel.EXTERNAL_MANUAL
