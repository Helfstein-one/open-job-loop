"""
Unit and integration tests for Model Context Protocol (MCP) ingestion subsystem.

Tests BaseJobIngestionClient contract, MockMcpJobClient replay and failure modes,
McpJobClient stdio subprocess integration, and payload parsing normalization.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from src.mcp.client import (
    BaseJobIngestionClient,
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

            async def fetch_jobs(self, limit: int = 10, **kwargs: Any) -> list[JobPosting]:
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
    def sample_jobs(self) -> list[dict[str, str]]:
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
