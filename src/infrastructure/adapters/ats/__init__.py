from __future__ import annotations
"""
ATS Adapters package.
"""

from src.infrastructure.adapters.ats.base import BaseATSAdapter, FormField, ApplicationResult
from src.infrastructure.adapters.ats.router import get_ats_adapter, detect_channel

__all__ = [
    "BaseATSAdapter",
    "FormField",
    "ApplicationResult",
    "get_ats_adapter",
    "detect_channel",
]
