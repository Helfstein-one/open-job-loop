"""
Terminal User Interface (UI) package for open-job-loop.

Provides:
- Banner: Predefined ASCII art banner and configuration panels.
- Console: Rich Live layout, DAG stage indicators, candidate cards, and metrics summaries.
"""

from src.ui.banner import (
    ASCII_ART,
    DEFAULT_DB,
    DEFAULT_MODEL,
    DEFAULT_VERSION,
    TAGLINE,
    get_banner_panel,
    get_banner_text,
    render_banner,
)
from src.ui.console import (
    STAGE_ICONS,
    STAGE_NAMES,
    BasePipelineUI,
    HeadlessPipelineUI,
    LivePipelineUI,
    UIState,
    build_current_job_panel,
    build_header_panel,
    build_layout,
    build_metrics_panel,
    build_pipeline_panel,
    create_pipeline_ui,
)

__all__ = [
    "ASCII_ART",
    "DEFAULT_DB",
    "DEFAULT_MODEL",
    "DEFAULT_VERSION",
    "STAGE_ICONS",
    "STAGE_NAMES",
    "TAGLINE",
    "BasePipelineUI",
    "HeadlessPipelineUI",
    "LivePipelineUI",
    "UIState",
    "build_current_job_panel",
    "build_header_panel",
    "build_layout",
    "build_metrics_panel",
    "build_pipeline_panel",
    "create_pipeline_ui",
    "get_banner_panel",
    "get_banner_text",
    "render_banner",
]
