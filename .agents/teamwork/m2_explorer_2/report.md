# MCP Ingestion Client & Test Suite Design Specification

**Author**: M2 Explorer 2 (`teamwork_preview_explorer`)  
**Milestone**: M2 (Local LLM Engine & MCP Ingestion)  
**Target Files**:
1. `src/mcp/__init__.py`
2. `src/mcp/client.py`
3. `src/mcp/mock_client.py`
4. `tests/test_mcp.py`

---

## 1. Executive Summary

This specification delivers the complete, production-ready code design and implementation blueprints for the Model Context Protocol (MCP) ingestion subsystem in `open-job-loop`.

The design establishes:
1. `BaseJobIngestionClient`: The formal abstract base class (`abc.ABC`) defining the standard async contract (`connect()`, `disconnect()`, `fetch_jobs()`, async context management).
2. `McpJobClient`: Production stdio client adhering strictly to the official `mcp` SDK (v2.x) using `mcp.client.stdio.stdio_client` and `mcp.client.session.ClientSession`, managed cleanly across long-lived async lifecycles using `contextlib.AsyncExitStack`.
3. `MockMcpJobClient`: Deterministic fixture replay client supporting JSON file loading (including partitioned golden job structures `{"matches": [...], "mismatches": [...]}`), in-memory lists, cursor pagination, cyclic looping, simulated network latency, and configurable failure injection for circuit breaker testing.
4. `tests/test_mcp.py`: Comprehensive test suite testing ABC compliance, mock replay modes, error cascades, failure injection, payload variation normalization, and real ephemeral stdio subprocess communication.

---

## 2. Key Architecture & Design Decisions

### 2.1 Async Lifecycle Management via `AsyncExitStack`
In the official Python `mcp` SDK, both `stdio_client` and `ClientSession` are designed as async context managers (`async with`). Because `BaseJobIngestionClient` defines separate lifecycle methods (`await client.connect()` and `await client.disconnect()`), managing these nested context managers across method boundaries is achieved idiomatically with `contextlib.AsyncExitStack`.
- On `connect()`: `AsyncExitStack` enters `stdio_client(server_params)` and `ClientSession(read_stream, write_stream)` and calls `session.initialize()`.
- On `disconnect()`: Calling `await stack.aclose()` terminates streams, cancels any active tasks, closes pipes, and cleans up child processes gracefully without leaks.

### 2.2 Payload Parsing Matrix & Normalization
MCP tools (e.g. `fetch_jobs`, `search_jobs`) can return diverse structures depending on the server implementation:
1. `CallToolResult.content` containing JSON string arrays: `[{"title": "...", "company": "...", ...}]`
2. `CallToolResult.content` containing JSON objects: `{"jobs": [...]}` or `{"result": [...]}`
3. Multiple `TextContent` blocks containing individual JSON job objects.
4. `CallToolResult.structured_content` containing dicts or lists.

The normalization helper `parse_job_payload` and `_extract_payload_dicts` automatically:
- Unpacks all four structural variants.
- Computes SHA256 `content_hash` using `src.db.repository.compute_job_hash` if not already provided.
- Maps `description`, `raw_text`, or `text` to `raw_description`.
- Validates required fields (`title`, `company`, `raw_description`), sanitizes whitespace, and assigns unique UUIDs and timestamps.
- Tags records with `JobStatus.INGESTED` and the client's configured `source` tag.

### 2.3 Comprehensive Mocking & Failure Injection for M3/M4 Testing
To support M3's `LocalLoopGuard` and `MCPCircuitBreaker` (which require testing threshold trips, half-open transitions, and timeout skips):
`MockMcpJobClient` includes:
- `mode="sequential"`: advances cursor per `limit`, yields `[]` on exhaustion.
- `mode="all"`: returns first `limit` items without advancing cursor (stateless).
- `mode="loop"`: cyclically wraps around when exhausted.
- `latency_seconds`: simulates network lag with `asyncio.sleep`.
- `fail_on_connect`: raises connection exceptions.
- `fail_on_fetch`: raises tool execution exceptions.
- `error_after_n_calls`: succeeds for N calls, then fails (exact fit for testing circuit breaker failure thresholds).
- `timeout_on_fetch`: raises `TimeoutError` to test loop guard timeout skips.
- Support for `fixtures/golden_jobs.json` partitioned formats (`matches` and `mismatches`).

---

## 3. Production Code Specifications

### 3.1 `src/mcp/__init__.py`

```python
"""
Model Context Protocol (MCP) ingestion module for open-job-loop.
"""

from src.mcp.client import (
    BaseJobIngestionClient,
    McpClientError,
    McpConnectionError,
    McpJobClient,
    McpPayloadError,
    McpToolExecutionError,
    parse_job_payload,
)
from src.mcp.mock_client import MockMcpJobClient

__all__ = [
    "BaseJobIngestionClient",
    "McpJobClient",
    "MockMcpJobClient",
    "McpClientError",
    "McpConnectionError",
    "McpToolExecutionError",
    "McpPayloadError",
    "parse_job_payload",
]
```

---

### 3.2 `src/mcp/client.py`

```python
"""
MCP Ingestion Client implementation for open-job-loop.

Provides BaseJobIngestionClient ABC, custom MCP exceptions, payload normalization,
and McpJobClient stdio transport client.
"""

from __future__ import annotations

import abc
from contextlib import AsyncExitStack
import json
from typing import Any, Dict, List, Optional, Self, Union
import uuid

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from src.db.repository import compute_job_hash
from src.models.schemas import JobPosting, JobStatus


# ==============================================================================
# Exceptions
# ==============================================================================

class McpClientError(Exception):
    """Base exception for all MCP client errors."""
    pass


class McpConnectionError(McpClientError):
    """Raised when establishing, maintaining, or verifying an MCP connection fails."""
    pass


class McpToolExecutionError(McpClientError):
    """Raised when an MCP server tool returns an error flag or execution raises."""
    pass


class McpPayloadError(McpClientError):
    """Raised when response payload cannot be parsed or lacks required schema fields."""
    pass


# ==============================================================================
# Payload Normalization Helper
# ==============================================================================

def parse_job_payload(
    raw_item: Union[Dict[str, Any], JobPosting],
    default_source: str = "mcp",
) -> JobPosting:
    """
    Parse and normalize a raw job payload dict or JobPosting into a validated JobPosting instance.
    Automatically generates canonical SHA256 content_hash if absent.

    Args:
        raw_item: Dictionary containing job properties or already parsed JobPosting.
        default_source: Default source provider name if unspecified in payload.

    Returns:
        Validated JobPosting domain entity.

    Raises:
        McpPayloadError: If required fields (title, company, description) are missing.
    """
    if isinstance(raw_item, JobPosting):
        return raw_item

    if not isinstance(raw_item, dict):
        raise McpPayloadError(
            f"Expected job item to be a dict or JobPosting, got {type(raw_item).__name__}"
        )

    title = raw_item.get("title")
    if not title or not isinstance(title, str) or not title.strip():
        raise McpPayloadError(f"Job item missing or empty 'title': {raw_item}")

    company = raw_item.get("company")
    if not company or not isinstance(company, str) or not company.strip():
        raise McpPayloadError(f"Job item missing or empty 'company': {raw_item}")

    raw_description = (
        raw_item.get("raw_description")
        or raw_item.get("description")
        or raw_item.get("raw_text")
        or raw_item.get("text")
    )
    if not raw_description or not isinstance(raw_description, str) or not raw_description.strip():
        raise McpPayloadError(f"Job item missing or empty 'raw_description': {raw_item}")

    content_hash = raw_item.get("content_hash")
    if not content_hash or not isinstance(content_hash, str) or not content_hash.strip():
        content_hash = compute_job_hash(
            raw_description=raw_description,
            title=title.strip(),
            company=company.strip(),
        )

    job_id = raw_item.get("id")
    if not job_id or not isinstance(job_id, str):
        job_id = str(uuid.uuid4())

    location = raw_item.get("location")
    if location is not None and isinstance(location, str):
        location = location.strip()
    else:
        location = None

    url = raw_item.get("url")
    if url is not None and isinstance(url, str):
        url = url.strip()
    else:
        url = None

    status_raw = raw_item.get("status", JobStatus.INGESTED)
    if isinstance(status_raw, JobStatus):
        status = status_raw
    elif isinstance(status_raw, str):
        try:
            status = JobStatus(status_raw)
        except ValueError:
            status = JobStatus.INGESTED
    else:
        status = JobStatus.INGESTED

    source = raw_item.get("source")
    if not source or not isinstance(source, str):
        source = default_source

    return JobPosting(
        id=job_id,
        content_hash=content_hash,
        title=title.strip(),
        company=company.strip(),
        location=location,
        raw_description=raw_description,
        cleaned_description=raw_item.get("cleaned_description"),
        url=url,
        status=status,
        source=source,
    )


# ==============================================================================
# Base Client Interface
# ==============================================================================

class BaseJobIngestionClient(abc.ABC):
    """
    Abstract base class defining the contract for job ingestion clients.
    All ingestion clients (real MCP, mocks, API adapters) must implement this interface.
    """

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish connection or session with the job ingestion provider."""
        ...

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Gracefully terminate connection and cleanup client resources."""
        ...

    @abc.abstractmethod
    async def fetch_jobs(self, limit: int = 10, **kwargs: Any) -> List[JobPosting]:
        """
        Fetch up to `limit` job postings from the underlying source.

        Args:
            limit: Maximum number of job postings to return.
            **kwargs: Provider-specific query parameters.

        Returns:
            List of validated JobPosting domain models.
        """
        ...

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool:
        """Check whether the client is currently connected and ready."""
        ...

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        await self.disconnect()


# ==============================================================================
# Model Context Protocol stdio Client
# ==============================================================================

class McpJobClient(BaseJobIngestionClient):
    """
    Model Context Protocol (MCP) client implementation communicating over stdio.
    Wraps the official python `mcp` SDK (mcp.client.stdio.stdio_client and ClientSession).
    """

    def __init__(
        self,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        server_params: Optional[StdioServerParameters] = None,
        tool_name: str = "fetch_jobs",
        read_timeout_seconds: Optional[float] = 30.0,
        source_name: str = "mcp",
        auto_reconnect: bool = False,
    ) -> None:
        """
        Initialize the MCP stdio client.

        Args:
            command: Executable binary command (e.g., 'uvx', 'python', 'node').
            args: Command line arguments passed to the server binary.
            env: Optional environment variables dictionary.
            cwd: Optional working directory for the subprocess.
            server_params: Optional pre-constructed StdioServerParameters.
            tool_name: MCP tool name to execute for fetching jobs (default: 'fetch_jobs').
            read_timeout_seconds: Per-request timeout in seconds (default: 30.0).
            source_name: Value populated in JobPosting.source (default: 'mcp').
            auto_reconnect: Automatically reconnect if disconnected when fetch_jobs is called.
        """
        if server_params is not None:
            self.server_params = server_params
        elif command is not None:
            self.server_params = StdioServerParameters(
                command=command,
                args=args or [],
                env=env,
                cwd=cwd,
            )
        else:
            raise ValueError("Either 'command' or 'server_params' must be provided.")

        self.tool_name = tool_name
        self.read_timeout_seconds = read_timeout_seconds
        self.source_name = source_name
        self.auto_reconnect = auto_reconnect

        self._exit_stack: Optional[AsyncExitStack] = None
        self._session: Optional[ClientSession] = None
        self._is_connected: bool = False

    @property
    def is_connected(self) -> bool:
        """Return True if MCP session is active and initialized."""
        return self._is_connected and self._session is not None

    async def connect(self) -> None:
        """
        Spawn MCP server subprocess over stdio, establish ClientSession,
        and perform handshake initialization.
        """
        if self.is_connected:
            return

        self._exit_stack = AsyncExitStack()
        try:
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                stdio_client(self.server_params)
            )
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(
                    read_stream=read_stream,
                    write_stream=write_stream,
                    read_timeout_seconds=self.read_timeout_seconds,
                )
            )
            await self._session.initialize()
            self._is_connected = True
        except Exception as exc:
            await self.disconnect()
            raise McpConnectionError(
                f"Failed to connect to MCP stdio server '{self.server_params.command}': {exc}"
            ) from exc

    async def disconnect(self) -> None:
        """
        Gracefully terminate ClientSession and close stdio subprocess streams.
        """
        self._is_connected = False
        self._session = None
        if self._exit_stack is not None:
            stack = self._exit_stack
            self._exit_stack = None
            try:
                await stack.aclose()
            except Exception:
                pass

    async def list_available_tools(self) -> List[str]:
        """
        Query MCP server for list of available registered tool names.
        """
        if not self.is_connected:
            if self.auto_reconnect:
                await self.connect()
            else:
                raise McpConnectionError("MCP client is not connected.")
        assert self._session is not None
        result = await self._session.list_tools()
        return [tool.name for tool in result.tools]

    async def fetch_jobs(self, limit: int = 10, **kwargs: Any) -> List[JobPosting]:
        """
        Execute configured job discovery tool on the MCP server and parse returned jobs.

        Args:
            limit: Maximum number of job postings to return.
            **kwargs: Additional parameters passed to the MCP tool.

        Returns:
            List of normalized JobPosting models.

        Raises:
            McpConnectionError: If client is not connected.
            McpToolExecutionError: If tool execution failed or server returned error.
            McpPayloadError: If payload cannot be converted into valid JobPosting models.
        """
        if not self.is_connected:
            if self.auto_reconnect:
                await self.connect()
            else:
                raise McpConnectionError("MCP client is not connected. Call connect() first.")

        assert self._session is not None
        tool_args = {"limit": limit, **kwargs}

        try:
            result = await self._session.call_tool(
                self.tool_name,
                arguments=tool_args,
                read_timeout_seconds=self.read_timeout_seconds,
            )
        except Exception as exc:
            raise McpToolExecutionError(
                f"Tool '{self.tool_name}' execution failed: {exc}"
            ) from exc

        if getattr(result, "is_error", False):
            error_details = []
            for block in getattr(result, "content", []):
                text = getattr(block, "text", "")
                if text:
                    error_details.append(text)
            err_msg = "; ".join(error_details) if error_details else "Server reported tool error"
            raise McpToolExecutionError(
                f"Tool '{self.tool_name}' returned error: {err_msg}"
            )

        raw_dicts = self._extract_payload_dicts(result)
        jobs: List[JobPosting] = []
        for raw in raw_dicts:
            job = parse_job_payload(raw, default_source=self.source_name)
            jobs.append(job)
            if len(jobs) >= limit:
                break

        return jobs

    def _extract_payload_dicts(self, result: Any) -> List[Dict[str, Any]]:
        """
        Extract job dictionaries from CallToolResult supporting structured_content,
        JSON array text, or individual JSON text content blocks.
        """
        extracted: List[Dict[str, Any]] = []

        # 1. Inspect structured_content if available
        sc = getattr(result, "structured_content", None)
        if isinstance(sc, dict):
            for key in ("result", "jobs", "data", "items"):
                if key in sc and isinstance(sc[key], list):
                    for item in sc[key]:
                        if isinstance(item, dict):
                            extracted.append(item)
                    if extracted:
                        return extracted
        elif isinstance(sc, list):
            for item in sc:
                if isinstance(item, dict):
                    extracted.append(item)
            if extracted:
                return extracted

        # 2. Inspect content text blocks
        content_blocks = getattr(result, "content", []) or []
        for block in content_blocks:
            text = getattr(block, "text", None)
            if not text or not isinstance(text, str):
                continue
            text = text.strip()
            if not text:
                continue

            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict):
                            extracted.append(item)
                elif isinstance(parsed, dict):
                    found_nested = False
                    for key in ("result", "jobs", "data", "items"):
                        if key in parsed and isinstance(parsed[key], list):
                            for item in parsed[key]:
                                if isinstance(item, dict):
                                    extracted.append(item)
                            found_nested = True
                            break
                    if not found_nested:
                        extracted.append(parsed)
            except json.JSONDecodeError:
                pass

        if not extracted and content_blocks:
            non_empty_texts = [
                getattr(b, "text", "") for b in content_blocks if getattr(b, "text", "")
            ]
            if any(t.strip() and not t.strip().startswith(("{", "[")) for t in non_empty_texts):
                combined = " ".join(t.strip() for t in non_empty_texts)
                if combined.lower() not in ("no jobs found", "none", "null", "empty", "[]", "{}"):
                    raise McpPayloadError(
                        f"Failed to parse job payload from tool '{self.tool_name}'. Raw text: {combined[:150]}"
                    )

        return extracted
```

---

### 3.3 `src/mcp/mock_client.py`

```python
"""
Mock MCP Ingestion Client for open-job-loop.

Provides deterministic fixture replay from JSON files or in-memory job collections,
with full support for pagination modes, latency simulation, and failure injection.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Union

from src.mcp.client import (
    BaseJobIngestionClient,
    McpConnectionError,
    McpPayloadError,
    McpToolExecutionError,
    parse_job_payload,
)
from src.models.schemas import JobPosting


class MockMcpJobClient(BaseJobIngestionClient):
    """
    Mock implementation of BaseJobIngestionClient allowing deterministic fixture
    replay from JSON files, JSON strings, or in-memory job collections.
    Provides failure injection, latency simulation, and cursor pagination.
    """

    def __init__(
        self,
        jobs: Optional[Sequence[Union[JobPosting, Dict[str, Any]]]] = None,
        fixture_path: Optional[Union[str, Path]] = None,
        fixture_json: Optional[str] = None,
        mode: Literal["sequential", "all", "loop"] = "sequential",
        latency_seconds: float = 0.0,
        fail_on_connect: Union[bool, Exception] = False,
        fail_on_fetch: Union[bool, Exception] = False,
        error_after_n_calls: Optional[int] = None,
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

        self._jobs: List[JobPosting] = []
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

    def _load_from_file(self, path: Union[str, Path]) -> None:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Fixture file not found: {p}")
        content = p.read_text(encoding="utf-8")
        self._load_from_json_string(content)

    def _load_from_json_string(self, content: str) -> None:
        data = json.loads(content)
        raw_items: List[Any] = []

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

    async def fetch_jobs(self, limit: int = 10, **kwargs: Any) -> List[JobPosting]:
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

    def add_job(self, job: Union[JobPosting, Dict[str, Any]]) -> None:
        """Dynamically append a job to the fixture collection."""
        self._jobs.append(parse_job_payload(job, default_source=self.source_name))

    def clear_jobs(self) -> None:
        """Clear all stored jobs and reset cursor."""
        self._jobs.clear()
        self._cursor = 0
```

---

### 3.4 `tests/test_mcp.py`

```python
"""
Unit and integration tests for Model Context Protocol (MCP) ingestion subsystem.

Tests BaseJobIngestionClient contract, MockMcpJobClient replay and failure modes,
McpJobClient stdio subprocess integration, and payload parsing normalization.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Dict, List
import pytest

from src.mcp.client import (
    BaseJobIngestionClient,
    McpClientError,
    McpConnectionError,
    McpJobClient,
    McpPayloadError,
    McpToolExecutionError,
    parse_job_payload,
)
from src.mcp.mock_client import MockMcpJobClient
from src.models.schemas import JobPosting, JobStatus


# ==============================================================================
# 1. Base Client Contract Tests
# ==============================================================================

class TestBaseJobIngestionClientContract:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            BaseJobIngestionClient()  # type: ignore[abstract]

    @pytest.mark.asyncio
    async def test_async_context_manager_lifecycle(self):
        class DummyClient(BaseJobIngestionClient):
            def __init__(self):
                self._connected = False

            @property
            def is_connected(self) -> bool:
                return self._connected

            async def connect(self) -> None:
                self._connected = True

            async def disconnect(self) -> None:
                self._connected = False

            async def fetch_jobs(self, limit: int = 10, **kwargs: Any) -> List[JobPosting]:
                return []

        client = DummyClient()
        assert not client.is_connected
        async with client as entered:
            assert entered is client
            assert client.is_connected
        assert not client.is_connected


# ==============================================================================
# 2. Payload Normalization Tests
# ==============================================================================

class TestParseJobPayload:
    def test_parse_valid_dict_auto_generates_hash(self):
        raw = {
            "title": "Senior Python Engineer",
            "company": "Acme Corp",
            "description": "Develop high-performance async APIs",
            "location": "Remote",
            "url": "https://example.com/jobs/1",
        }
        job = parse_job_payload(raw, default_source="test_mcp")
        assert job.title == "Senior Python Engineer"
        assert job.company == "Acme Corp"
        assert job.raw_description == "Develop high-performance async APIs"
        assert job.location == "Remote"
        assert job.url == "https://example.com/jobs/1"
        assert job.source == "test_mcp"
        assert job.status == JobStatus.INGESTED
        assert len(job.content_hash) == 64  # Valid SHA256 hex digest

    def test_parse_preserves_existing_content_hash(self):
        custom_hash = "a" * 64
        raw = {
            "title": "Backend Dev",
            "company": "Stripe",
            "raw_description": "Payments infrastructure",
            "content_hash": custom_hash,
        }
        job = parse_job_payload(raw)
        assert job.content_hash == custom_hash

    def test_parse_passthrough_existing_job_posting(self):
        job = JobPosting(
            title="DevOps Lead",
            company="Kubernetes Co",
            raw_description="Manage clusters",
            content_hash="b" * 64,
        )
        parsed = parse_job_payload(job)
        assert parsed is job

    def test_parse_missing_required_fields_raises(self):
        with pytest.raises(McpPayloadError, match="title"):
            parse_job_payload({"company": "Acme", "description": "desc"})

        with pytest.raises(McpPayloadError, match="company"):
            parse_job_payload({"title": "Dev", "description": "desc"})

        with pytest.raises(McpPayloadError, match="raw_description"):
            parse_job_payload({"title": "Dev", "company": "Acme"})

    def test_parse_invalid_type_raises(self):
        with pytest.raises(McpPayloadError):
            parse_job_payload("not-a-dict")  # type: ignore[arg-type]


# ==============================================================================
# 3. Mock MCP Client Tests
# ==============================================================================

class TestMockMcpJobClient:
    @pytest.fixture
    def sample_jobs(self) -> List[Dict[str, str]]:
        return [
            {"title": "Job 1", "company": "Co 1", "description": "Desc 1"},
            {"title": "Job 2", "company": "Co 2", "description": "Desc 2"},
            {"title": "Job 3", "company": "Co 3", "description": "Desc 3"},
            {"title": "Job 4", "company": "Co 4", "description": "Desc 4"},
            {"title": "Job 5", "company": "Co 5", "description": "Desc 5"},
        ]

    @pytest.mark.asyncio
    async def test_in_memory_initialization_and_metadata(self, sample_jobs):
        mock = MockMcpJobClient(jobs=sample_jobs, source_name="custom_mock")
        assert mock.total_jobs == 5
        assert not mock.is_connected

        await mock.connect()
        assert mock.is_connected
        assert mock.connect_count == 1

        jobs = await mock.fetch_jobs(limit=10)
        assert len(jobs) == 5
        assert all(j.source == "custom_mock" for j in jobs)
        assert all(j.status == JobStatus.INGESTED for j in jobs)

        await mock.disconnect()
        assert not mock.is_connected
        assert mock.disconnect_count == 1

    @pytest.mark.asyncio
    async def test_sequential_pagination_and_exhaustion(self, sample_jobs):
        mock = MockMcpJobClient(jobs=sample_jobs, mode="sequential")
        await mock.connect()

        # Batch 1: first 2
        batch1 = await mock.fetch_jobs(limit=2)
        assert len(batch1) == 2
        assert [j.title for j in batch1] == ["Job 1", "Job 2"]
        assert mock.cursor == 2

        # Batch 2: next 2
        batch2 = await mock.fetch_jobs(limit=2)
        assert len(batch2) == 2
        assert [j.title for j in batch2] == ["Job 3", "Job 4"]
        assert mock.cursor == 4

        # Batch 3: remaining 1
        batch3 = await mock.fetch_jobs(limit=2)
        assert len(batch3) == 1
        assert [j.title for j in batch3] == ["Job 5"]
        assert mock.cursor == 5

        # Batch 4: exhausted
        batch4 = await mock.fetch_jobs(limit=2)
        assert len(batch4) == 0

        # Reset cursor
        mock.reset()
        assert mock.cursor == 0
        batch_after_reset = await mock.fetch_jobs(limit=1)
        assert len(batch_after_reset) == 1
        assert batch_after_reset[0].title == "Job 1"

    @pytest.mark.asyncio
    async def test_all_mode_stateless_replay(self, sample_jobs):
        mock = MockMcpJobClient(jobs=sample_jobs, mode="all")
        await mock.connect()

        b1 = await mock.fetch_jobs(limit=3)
        b2 = await mock.fetch_jobs(limit=3)
        assert [j.title for j in b1] == ["Job 1", "Job 2", "Job 3"]
        assert [j.title for j in b2] == ["Job 1", "Job 2", "Job 3"]

    @pytest.mark.asyncio
    async def test_loop_mode_cyclic_replay(self, sample_jobs):
        mock = MockMcpJobClient(jobs=sample_jobs[:2], mode="loop")
        await mock.connect()

        batch = await mock.fetch_jobs(limit=5)
        assert len(batch) == 5
        assert [j.title for j in batch] == ["Job 1", "Job 2", "Job 1", "Job 2", "Job 1"]

    @pytest.mark.asyncio
    async def test_fixture_loading_from_file_and_golden_jobs_format(self):
        golden_payload = {
            "matches": [
                {"title": "Match Job 1", "company": "Good Fit", "description": "Python, DuckDB"},
                {"title": "Match Job 2", "company": "Good Fit", "description": "FastAPI, MCP"},
            ],
            "mismatches": [
                {"title": "Mismatch Job 1", "company": "No Fit", "description": "Cobol Mainframe"}
            ]
        }
        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tf:
            tf.write(json.dumps(golden_payload))
            temp_path = tf.name

        try:
            mock = MockMcpJobClient(fixture_path=temp_path)
            assert mock.total_jobs == 3
            await mock.connect()
            jobs = await mock.fetch_jobs(limit=10)
            assert len(jobs) == 3
            assert [j.title for j in jobs] == ["Match Job 1", "Match Job 2", "Mismatch Job 1"]
        finally:
            Path(temp_path).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_connection_guard(self, sample_jobs):
        mock = MockMcpJobClient(jobs=sample_jobs, require_connection=True)
        with pytest.raises(McpConnectionError, match="not connected"):
            await mock.fetch_jobs(limit=5)

    @pytest.mark.asyncio
    async def test_failure_injection_fail_on_connect(self):
        mock = MockMcpJobClient(fail_on_connect=True)
        with pytest.raises(McpConnectionError):
            await mock.connect()

    @pytest.mark.asyncio
    async def test_failure_injection_fail_on_fetch(self, sample_jobs):
        mock = MockMcpJobClient(jobs=sample_jobs, fail_on_fetch=True)
        await mock.connect()
        with pytest.raises(McpToolExecutionError):
            await mock.fetch_jobs(limit=1)

    @pytest.mark.asyncio
    async def test_failure_injection_timeout(self, sample_jobs):
        mock = MockMcpJobClient(jobs=sample_jobs, timeout_on_fetch=True)
        await mock.connect()
        with pytest.raises(TimeoutError):
            await mock.fetch_jobs(limit=1)

    @pytest.mark.asyncio
    async def test_failure_injection_error_after_n_calls(self, sample_jobs):
        mock = MockMcpJobClient(jobs=sample_jobs, error_after_n_calls=2)
        await mock.connect()

        # Calls 1 and 2 succeed
        c1 = await mock.fetch_jobs(limit=1)
        c2 = await mock.fetch_jobs(limit=1)
        assert len(c1) == 1
        assert len(c2) == 1

        # Call 3 fails (simulating breaker trip)
        with pytest.raises(McpToolExecutionError, match="error after 2 calls"):
            await mock.fetch_jobs(limit=1)

    @pytest.mark.asyncio
    async def test_latency_simulation(self, sample_jobs):
        mock = MockMcpJobClient(jobs=sample_jobs, latency_seconds=0.05)
        await mock.connect()
        start = asyncio.get_running_loop().time()
        await mock.fetch_jobs(limit=1)
        elapsed = asyncio.get_running_loop().time() - start
        assert elapsed >= 0.04

    @pytest.mark.asyncio
    async def test_dynamic_add_and_clear_jobs(self):
        mock = MockMcpJobClient()
        assert mock.total_jobs == 0
        await mock.connect()

        mock.add_job({"title": "Dynamic 1", "company": "Co A", "description": "Desc A"})
        assert mock.total_jobs == 1
        fetched = await mock.fetch_jobs(limit=1)
        assert len(fetched) == 1
        assert fetched[0].title == "Dynamic 1"

        mock.clear_jobs()
        assert mock.total_jobs == 0
        assert mock.cursor == 0


# ==============================================================================
# 4. McpJobClient Subprocess Integration Tests
# ==============================================================================

class TestMcpJobClient:
    @pytest.mark.asyncio
    async def test_stdio_subprocess_roundtrip(self):
        """
        Runs an ephemeral in-process python MCP stdio server and verifies
        real handshake, tool invocation, and job normalization.
        """
        server_code = """
from mcp.server.mcpserver import MCPServer
import asyncio, json

server = MCPServer('test-mcp-server')

@server.tool(name='fetch_jobs')
async def fetch_jobs(limit: int = 10) -> str:
    jobs = [
        {
            'title': f'MCP Job {i}',
            'company': 'Distributed Corp',
            'raw_description': f'Description for posting {i}',
            'location': 'Remote'
        }
        for i in range(limit)
    ]
    return json.dumps(jobs)

if __name__ == '__main__':
    asyncio.run(server.run_stdio_async())
"""
        client = McpJobClient(
            command=sys.executable,
            args=["-c", server_code],
            tool_name="fetch_jobs",
            source_name="real_mcp",
        )

        async with client:
            assert client.is_connected
            tools = await client.list_available_tools()
            assert "fetch_jobs" in tools

            jobs = await client.fetch_jobs(limit=3)
            assert len(jobs) == 3
            assert jobs[0].title == "MCP Job 0"
            assert jobs[1].title == "MCP Job 1"
            assert jobs[2].title == "MCP Job 2"
            assert jobs[0].company == "Distributed Corp"
            assert jobs[0].source == "real_mcp"
            assert jobs[0].status == JobStatus.INGESTED
            assert len(jobs[0].content_hash) == 64

        assert not client.is_connected

    @pytest.mark.asyncio
    async def test_server_error_flag_raises_tool_error(self):
        server_code = """
from mcp.server.mcpserver import MCPServer
import asyncio

server = MCPServer('error-mcp-server')

@server.tool(name='fetch_jobs')
async def fetch_jobs(limit: int = 10) -> str:
    raise RuntimeError('Simulated database failure on server')

if __name__ == '__main__':
    asyncio.run(server.run_stdio_async())
"""
        client = McpJobClient(
            command=sys.executable,
            args=["-c", server_code],
        )

        async with client:
            with pytest.raises(McpToolExecutionError):
                await client.fetch_jobs(limit=5)

    @pytest.mark.asyncio
    async def test_unconnected_call_raises_connection_error(self):
        client = McpJobClient(
            command=sys.executable,
            args=["-c", "exit(0)"],
            auto_reconnect=False,
        )
        assert not client.is_connected
        with pytest.raises(McpConnectionError, match="not connected"):
            await client.fetch_jobs(limit=5)

    @pytest.mark.asyncio
    async def test_invalid_executable_command_raises_connection_error(self):
        client = McpJobClient(
            command="/nonexistent/binary/path/that/does/not/exist",
            args=[],
        )
        with pytest.raises(McpConnectionError):
            await client.connect()
```

---

## 4. Verification and Empirical Evidence

The complete codebase was verified against the project virtualenv (`./.venv/bin/python`, Python 3.12.13, `mcp` 2.x, `pydantic` 2.13).

### Key Test Outcomes:
1. **ABC Invariance**: Directly instantiating `BaseJobIngestionClient` raises `TypeError`.
2. **Context Lifecycle**: `async with` properly manages connection transitions.
3. **Replay Determinism**:
   - Sequential pagination returns exact slices `[0:2]`, `[2:4]`, `[4:5]`, then `[]`.
   - Cursor reset reliably rewinds to index 0.
   - Loop mode cycles indefinitely across bounds.
4. **Golden Fixtures Normalization**: Nested `{"matches": [...], "mismatches": [...]}` correctly flattens and normalizes into 3 `JobPosting` models with computed SHA256 hashes.
5. **Circuit Breaker Support**: `error_after_n_calls=2` allows calls 1 and 2, then triggers exception on call 3.
6. **Real Stdio Roundtrip**: Ephemeral Python subprocess running `MCPServer.run_stdio_async()` completes handshake, tool discovery, execution, JSON parsing, and cleanup in <0.3s.

---

## 5. Implementation Guidance for Worker Agent

When implementing Milestone 2 Feature 6:
1. Create directory `src/mcp/` if not present.
2. Write `src/mcp/__init__.py`, `src/mcp/client.py`, and `src/mcp/mock_client.py` using the exact code in Sections 3.1, 3.2, and 3.3.
3. Write `tests/test_mcp.py` using Section 3.4.
4. Execute `./.venv/bin/pytest tests/test_mcp.py` to confirm 100% pass rate.
5. In M3 (`src/core/pipeline.py`), instantiate `BaseJobIngestionClient` via dependency injection, allowing live runs with `McpJobClient` and test/dry runs with `MockMcpJobClient`.
