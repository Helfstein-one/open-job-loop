"""
Core pipeline and execution harness package for open-job-loop.
"""

from src.core.harness import (
    CircuitOpenError,
    CircuitState,
    HarnessError,
    LocalLoopGuard,
    MaxIterationsReachedError,
    MCPCircuitBreaker,
)
from src.core.pipeline import (
    DEFAULT_CANDIDATE_PROFILE,
    JobPipeline,
    PipelineConfig,
    PipelineEvent,
    PipelineEventType,
    PipelineResult,
)
from src.core.truncator import (
    DescriptionTooShortError,
    TextTruncator,
    TruncationResult,
)

__all__ = [
    "DEFAULT_CANDIDATE_PROFILE",
    "CircuitOpenError",
    "CircuitState",
    "DescriptionTooShortError",
    "HarnessError",
    "JobPipeline",
    "LocalLoopGuard",
    "MCPCircuitBreaker",
    "MaxIterationsReachedError",
    "PipelineConfig",
    "PipelineEvent",
    "PipelineEventType",
    "PipelineResult",
    "TextTruncator",
    "TruncationResult",
]
