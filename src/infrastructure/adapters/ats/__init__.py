from __future__ import annotations

"""
ATS Adapters package.
"""

from src.infrastructure.adapters.ats.base import ApplicationResult, BaseATSAdapter, FormField
from src.infrastructure.adapters.ats.router import detect_channel, get_ats_adapter

__all__ = [
    "BaseATSAdapter",
    "FormField",
    "ApplicationResult",
    "get_ats_adapter",
    "detect_channel",
]
