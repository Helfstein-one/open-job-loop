"""
MCP Ingestion Client implementation for open-job-loop.

Provides BaseJobIngestionClient ABC, custom MCP exceptions, payload normalization,
and McpJobClient stdio transport client.
"""

from __future__ import annotations

import abc
import json
import logging
import types
import uuid
from contextlib import AsyncExitStack
from typing import Any, Self

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from src.db.repository import compute_job_hash
from src.models.schemas import JobPosting, JobStatus

logger = logging.getLogger(__name__)

# ==============================================================================
# Exceptions
# ==============================================================================

class McpClientError(Exception):
    """Base exception for all MCP client errors."""


class McpConnectionError(McpClientError):
    """Raised when establishing, maintaining, or verifying an MCP connection fails."""


class McpToolExecutionError(McpClientError):
    """Raised when an MCP server tool returns an error flag or execution raises."""


class McpPayloadError(McpClientError):
    """Raised when response payload cannot be parsed or lacks required schema fields."""


# ==============================================================================
# Payload Normalization Helper
# ==============================================================================

def parse_job_payload(
    raw_item: dict[str, Any] | JobPosting,
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
    async def fetch_jobs(self, limit: int = 10, **kwargs: Any) -> list[JobPosting]:
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
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
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
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        server_params: StdioServerParameters | None = None,
        tool_name: str = "fetch_jobs",
        read_timeout_seconds: float | None = 30.0,
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

        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._is_connected: bool = False

        self._raw_read_stream = None
        self._raw_write_stream = None

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
            self._raw_read_stream = read_stream
            self._raw_write_stream = write_stream
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
        self._raw_read_stream = None
        self._raw_write_stream = None
        if self._exit_stack is not None:
            stack = self._exit_stack
            self._exit_stack = None
            try:
                await stack.aclose()
            except (OSError, RuntimeError, TimeoutError) as exc:
                logger.debug("Error while closing MCP exit stack: %s", exc)

    async def list_available_tools(self) -> list[str]:
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

    async def fetch_jobs(self, limit: int = 10, **kwargs: Any) -> list[JobPosting]:
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
        jobs: list[JobPosting] = []
        for raw in raw_dicts:
            job = parse_job_payload(raw, default_source=self.source_name)
            jobs.append(job)
            if len(jobs) >= limit:
                break

        return jobs

    def _extract_payload_dicts(self, result: Any) -> list[dict[str, Any]]:
        """
        Extract job dictionaries from CallToolResult supporting structured_content,
        JSON array text, or individual JSON text content blocks.
        """
        extracted: list[dict[str, Any]] = []

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
