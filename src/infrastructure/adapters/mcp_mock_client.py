from __future__ import annotations
"""
Mock MCP Ingestion Client for open-job-loop.

Provides deterministic fixture replay from JSON files or in-memory job collections,
with full support for pagination modes, latency simulation, and failure injection.
"""


import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from src.infrastructure.adapters.mcp_client import (
    BaseJobIngestionClient,
    McpConnectionError,
    McpPayloadError,
    McpToolExecutionError,
    parse_job_payload,
)
from src.domain.models import JobPosting


class MockMcpJobClient(BaseJobIngestionClient):
    """
    Mock implementation of BaseJobIngestionClient allowing deterministic fixture
    replay from JSON files, JSON strings, or in-memory job collections.
    Provides failure injection, latency simulation, and cursor pagination.
    """

    def __init__(
        self,
        jobs: Sequence[JobPosting | dict[str, Any]] | None = None,
        fixture_path: str | Path | None = None,
        fixture_json: str | None = None,
        mode: Literal["sequential", "all", "loop"] = "sequential",
        latency_seconds: float = 0.0,
        fail_on_connect: bool | Exception = False,
        fail_on_fetch: bool | Exception = False,
        error_after_n_calls: int | None = None,
        timeout_on_fetch: bool = False,
        source_name: str = "mock",
        require_connection: bool = True,
    ) -> None:
        """
        Initialize the Mock MCP client.

        Args:
            jobs: In-memory sequence of JobPosting instances or raw job dicts.
            fixture_path: Path to a JSON file containing job definitions.
            fixture_json: Raw JSON string containing job definitions.
            mode: Replay mode:
                  - 'sequential': advance cursor, return empty when exhausted (default).
                  - 'all': return first min(limit, N) jobs without advancing cursor.
                  - 'loop': wrap around cyclically when exhausted.
            latency_seconds: Artificial async delay (asyncio.sleep) per fetch call.
            fail_on_connect: If True or Exception, raises error on connect().
            fail_on_fetch: If True or Exception, raises error on fetch_jobs().
            error_after_n_calls: Succeeds for first N fetch calls, then raises error (for circuit breaker tests).
            timeout_on_fetch: If True, raises TimeoutError on fetch_jobs().
            source_name: Value populated in JobPosting.source (default: 'mock').
            require_connection: If True, fetch_jobs raises if not connected.
        """
        self.mode = mode
        self.latency_seconds = latency_seconds
        self.fail_on_connect = fail_on_connect
        self.fail_on_fetch = fail_on_fetch
        self.error_after_n_calls = error_after_n_calls
        self.timeout_on_fetch = timeout_on_fetch
        self.source_name = source_name
        self.require_connection = require_connection

        self._jobs: list[JobPosting] = []
        self._cursor: int = 0
        self._is_connected: bool = False
        self.call_count: int = 0
        self.fetch_count: int = 0
        self.connect_count: int = 0
        self.disconnect_count: int = 0

        # Load fixture data
        if fixture_path is not None:
            self._load_from_file(fixture_path)
        elif fixture_json is not None:
            self._load_from_json_string(fixture_json)
        elif jobs is not None:
            for item in jobs:
                self._jobs.append(parse_job_payload(item, default_source=self.source_name))

    def _load_from_file(self, path: str | Path) -> None:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Fixture file not found: {p}")
        content = p.read_text(encoding="utf-8")
        self._load_from_json_string(content)

    def _load_from_json_string(self, content: str) -> None:
        data = json.loads(content)
        raw_items: list[Any] = []

        if isinstance(data, list):
            raw_items = data
        elif isinstance(data, dict):
            # Support golden jobs partitioned format: {"matches": [...], "mismatches": [...]}
            if "matches" in data or "mismatches" in data:
                raw_items = list(data.get("matches", [])) + list(data.get("mismatches", []))
            elif "jobs" in data and isinstance(data["jobs"], list):
                raw_items = data["jobs"]
            elif "golden_jobs" in data and isinstance(data["golden_jobs"], list):
                raw_items = data["golden_jobs"]
            elif "data" in data and isinstance(data["data"], list):
                raw_items = data["data"]
            else:
                raw_items = [data]
        else:
            raise McpPayloadError(
                f"Fixture must contain a JSON array or object, got {type(data).__name__}"
            )

        for item in raw_items:
            self._jobs.append(parse_job_payload(item, default_source=self.source_name))

    @property
    def is_connected(self) -> bool:
        """Return True if mock client is connected."""
        return self._is_connected

    @property
    def cursor(self) -> int:
        """Current index position within the job fixture pool."""
        return self._cursor

    @property
    def total_jobs(self) -> int:
        """Total number of jobs loaded into the mock fixture pool."""
        return len(self._jobs)

    async def connect(self) -> None:
        """
        Simulate establishing connection.
        """
        if self.fail_on_connect:
            if isinstance(self.fail_on_connect, Exception):
                raise self.fail_on_connect
            raise McpConnectionError("Simulated connection error in MockMcpJobClient")

        self._is_connected = True
        self.connect_count += 1

    async def disconnect(self) -> None:
        """
        Simulate closing connection.
        """
        self._is_connected = False
        self.disconnect_count += 1

    async def fetch_jobs(self, limit: int = 10, **kwargs: Any) -> list[JobPosting]:
        """
        Simulate fetching jobs from fixture pool according to configured replay mode.

        Args:
            limit: Maximum number of jobs to return.

        Returns:
            List of JobPosting models.
        """
        if self.require_connection and not self._is_connected:
            raise McpConnectionError("Mock client is not connected. Call connect() before fetch_jobs().")

        if self.fail_on_fetch:
            if isinstance(self.fail_on_fetch, Exception):
                raise self.fail_on_fetch
            raise McpToolExecutionError("Simulated fetch error in MockMcpJobClient")

        if self.timeout_on_fetch:
            raise TimeoutError("Simulated TimeoutError in MockMcpJobClient.fetch_jobs()")

        if self.error_after_n_calls is not None and self.fetch_count >= self.error_after_n_calls:
            raise McpToolExecutionError(
                f"Simulated error after {self.error_after_n_calls} calls (current call #{self.fetch_count + 1})"
            )

        if self.latency_seconds > 0:
            await asyncio.sleep(self.latency_seconds)

        self.fetch_count += 1
        self.call_count += 1

        if not self._jobs:
            return []

        if self.mode == "all":
            return list(self._jobs[:limit])

        elif self.mode == "sequential":
            if self._cursor >= len(self._jobs):
                return []
            end = min(self._cursor + limit, len(self._jobs))
            batch = self._jobs[self._cursor:end]
            self._cursor = end
            return list(batch)

        elif self.mode == "loop":
            batch = []
            for _ in range(limit):
                batch.append(self._jobs[self._cursor % len(self._jobs)])
                self._cursor = (self._cursor + 1) % len(self._jobs)
            return batch

        else:
            raise ValueError(f"Unknown replay mode '{self.mode}'")

    def reset(self) -> None:
        """Reset pagination cursor and call counters to initial state."""
        self._cursor = 0
        self.call_count = 0
        self.fetch_count = 0

    def add_job(self, job: JobPosting | dict[str, Any]) -> None:
        """Dynamically append a job to the fixture collection."""
        self._jobs.append(parse_job_payload(job, default_source=self.source_name))

    def clear_jobs(self) -> None:
        """Clear all stored jobs and reset cursor."""
        self._jobs.clear()
        self._cursor = 0
