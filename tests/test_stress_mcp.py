"""
Empirical stress tests for Milestone M2 MCP Ingestion subsystem.

Stress-tests:
- stdio connection hangs during handshake and tool execution
- Abrupt subprocess terminations (SIGTERM and SIGKILL)
- Orphan child process leakage checks
- Mock client failure modes and custom exception injection
- Pagination loops, boundary limits, and concurrent access
- High-volume and large-payload transfers (1,000 jobs, 5MB descriptions)
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

import pytest

from src.infrastructure.adapters.mcp_client import (
    McpConnectionError,
    McpJobClient,
    McpPayloadError,
    McpToolExecutionError,
    parse_job_payload,
)
from src.infrastructure.adapters.mcp_mock_client import MockMcpJobClient
from src.domain.models import JobStatus

# ==============================================================================
# Helper to get child PIDs for leakage verification
# ==============================================================================

def get_child_pids() -> list[int]:
    """Retrieve child process PIDs of current process on macOS / Unix."""
    try:
        res = subprocess.run(
            ["pgrep", "-P", str(os.getpid())],
            capture_output=True,
            text=True,
            check=False,
        )
        return [int(p) for p in res.stdout.strip().split() if p]
    except OSError:
        return []


# ==============================================================================
# 1. Stdio Connection Hangs & Process Leakage
# ==============================================================================

class TestStdioConnectionHangs:
    @pytest.mark.asyncio
    async def test_handshake_hang_times_out_and_cleans_child_processes(self):
        """
        Verify that if an MCP server subprocess hangs during handshake (initialize),
        the client raises McpConnectionError within read_timeout_seconds and does not leak orphans.
        """
        # Server starts but never responds or speaks MCP protocol
        client = McpJobClient(
            command=sys.executable,
            args=["-c", "import time; time.sleep(60)"],
            read_timeout_seconds=1.0,
        )

        with pytest.raises(McpConnectionError, match="timed out"):
            await client.connect()

        assert not client.is_connected

        # Allow OS scheduler to reap exited processes
        await asyncio.sleep(0.5)
        orphans = get_child_pids()
        assert len(orphans) == 0, f"Leaked orphan processes: {orphans}"

    @pytest.mark.asyncio
    async def test_tool_call_hang_times_out_and_disconnects_cleanly(self):
        """
        Verify that if an MCP server tool call hangs indefinitely,
        fetch_jobs raises McpToolExecutionError within read_timeout_seconds,
        and exiting the context cleans up the subprocess cleanly.
        """
        server_code = """
from mcp.server.mcpserver import MCPServer
import asyncio

server = MCPServer('hang-tool-server')

@server.tool(name='fetch_jobs')
async def fetch_jobs(limit: int = 10) -> str:
    await asyncio.sleep(60)
    return '[]'

if __name__ == '__main__':
    asyncio.run(server.run_stdio_async())
"""
        client = McpJobClient(
            command=sys.executable,
            args=["-c", server_code],
            read_timeout_seconds=1.0,
        )

        async with client:
            assert client.is_connected
            with pytest.raises(McpToolExecutionError, match="timed out"):
                await client.fetch_jobs(limit=5)

        assert not client.is_connected
        await asyncio.sleep(0.5)
        orphans = get_child_pids()
        assert len(orphans) == 0, f"Leaked orphan processes after tool hang: {orphans}"


# ==============================================================================
# 2. Abrupt Subprocess Terminations (SIGTERM & SIGKILL)
# ==============================================================================

class TestAbruptSubprocessTerminations:
    @pytest.mark.asyncio
    async def test_sigterm_before_fetch_raises_and_cleans_up(self):
        """
        Verify behavior when the server subprocess receives SIGTERM prior to tool invocation.
        """
        server_code = """
from mcp.server.mcpserver import MCPServer
import asyncio

server = MCPServer('sigterm-test')

@server.tool(name='fetch_jobs')
async def fetch_jobs(limit: int = 10) -> str:
    return '[]'

if __name__ == '__main__':
    asyncio.run(server.run_stdio_async())
"""
        client = McpJobClient(
            command=sys.executable,
            args=["-c", server_code],
        )
        await client.connect()
        assert client.is_connected

        children = get_child_pids()
        assert len(children) >= 1, "Child process not detected"
        child_pid = children[0]

        # Terminate server abruptly via SIGTERM
        os.kill(child_pid, signal.SIGTERM)
        await asyncio.sleep(0.3)

        with pytest.raises(McpToolExecutionError):
            await client.fetch_jobs(limit=1)

        await client.disconnect()
        assert not client.is_connected

    @pytest.mark.asyncio
    async def test_sigkill_in_flight_call_raises_cleanly(self):
        """
        Verify behavior when the server subprocess is violently killed (SIGKILL)
        while a tool call is actively awaiting results.
        """
        server_code = """
from mcp.server.mcpserver import MCPServer
import asyncio

server = MCPServer('sigkill-in-flight-test')

@server.tool(name='fetch_jobs')
async def fetch_jobs(limit: int = 10) -> str:
    await asyncio.sleep(10)
    return '[]'

if __name__ == '__main__':
    asyncio.run(server.run_stdio_async())
"""
        client = McpJobClient(
            command=sys.executable,
            args=["-c", server_code],
        )
        await client.connect()
        assert client.is_connected

        children = get_child_pids()
        assert len(children) >= 1
        child_pid = children[0]

        async def kill_delayed():
            await asyncio.sleep(0.3)
            os.kill(child_pid, signal.SIGKILL)

        killer_task = asyncio.create_task(kill_delayed())

        with pytest.raises(McpToolExecutionError):
            await client.fetch_jobs(limit=1)

        await killer_task
        await client.disconnect()
        assert not client.is_connected
        await asyncio.sleep(0.3)
        assert len(get_child_pids()) == 0


# ==============================================================================
# 3. High Volume & Large Payload Transfers
# ==============================================================================

class TestLargePayloadTransfers:
    @pytest.mark.asyncio
    async def test_stdio_1000_jobs_stream_without_pipe_deadlock(self):
        """
        Verify that streaming a large payload of 1,000 job postings (~1MB JSON)
        over stdio completes without deadlocking the pipe buffer.
        """
        server_code = """
from mcp.server.mcpserver import MCPServer
import asyncio, json

server = MCPServer('1000-jobs-server')

@server.tool(name='fetch_jobs')
async def fetch_jobs(limit: int = 1000) -> str:
    jobs = [
        {
            'title': f'Distributed Engineer {i}',
            'company': f'Enterprise Corp {i % 10}',
            'raw_description': f'Extensive job responsibilities description block number {i}. ' * 10,
            'location': 'Remote',
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
            read_timeout_seconds=20.0,
        )

        async with client:
            jobs = await client.fetch_jobs(limit=1000)
            assert len(jobs) == 1000
            assert jobs[0].title == "Distributed Engineer 0"
            assert jobs[999].title == "Distributed Engineer 999"
            assert all(len(j.content_hash) == 64 for j in jobs)
            # Verify uniqueness of hashes
            hashes = {j.content_hash for j in jobs}
            assert len(hashes) == 1000

    @pytest.mark.asyncio
    async def test_stdio_single_5mb_job_description(self):
        """
        Verify that a single job posting with a massive 5MB raw description
        is correctly transported, hash-calculated, and preserved.
        """
        server_code = """
from mcp.server.mcpserver import MCPServer
import asyncio, json

server = MCPServer('huge-job-server')

@server.tool(name='fetch_jobs')
async def fetch_jobs(limit: int = 1) -> str:
    huge_text = 'Z' * (5 * 1024 * 1024)
    jobs = [{
        'title': 'Senior Mega Data Architect',
        'company': 'Massive Corp',
        'raw_description': huge_text,
    }]
    return json.dumps(jobs)

if __name__ == '__main__':
    asyncio.run(server.run_stdio_async())
"""
        client = McpJobClient(
            command=sys.executable,
            args=["-c", server_code],
            read_timeout_seconds=20.0,
        )

        async with client:
            jobs = await client.fetch_jobs(limit=1)
            assert len(jobs) == 1
            assert jobs[0].title == "Senior Mega Data Architect"
            assert len(jobs[0].raw_description) == 5 * 1024 * 1024
            assert len(jobs[0].content_hash) == 64


# ==============================================================================
# 4. Adversarial Payloads & Edge Cases
# ==============================================================================

class TestAdversarialPayloads:
    def test_unicode_special_characters_and_null_bytes(self):
        """
        Verify normalization and hashing under complex unicode, emojis, and null bytes.
        """
        raw = {
            "title": "Principal Engineer 🚀 (AI/ML) 🤖",
            "company": "DeepTech \u200b Corp \ufeff",
            "raw_description": "Building next-gen models with RTL: بِسْمِ اللَّهِ and null \x00 byte handling",
            "location": "Global / Remote 🌍",
        }
        job = parse_job_payload(raw)
        assert "🚀" in job.title
        assert len(job.content_hash) == 64
        assert job.status == JobStatus.INGESTED

    def test_payload_missing_whitespace_only_fields(self):
        """
        Verify that whitespace-only fields are treated as empty and rejected.
        """
        with pytest.raises(McpPayloadError, match="title"):
            parse_job_payload({"title": "   \t\n  ", "company": "Acme", "description": "Valid desc"})

        with pytest.raises(McpPayloadError, match="company"):
            parse_job_payload({"title": "Valid title", "company": "   ", "description": "Valid desc"})

        with pytest.raises(McpPayloadError, match="raw_description"):
            parse_job_payload({"title": "Valid title", "company": "Acme", "description": "    "})

    def test_invalid_type_and_none_rejection(self):
        """
        Verify non-dict types are rejected with McpPayloadError.
        """
        with pytest.raises(McpPayloadError):
            parse_job_payload(None)  # type: ignore[arg-type]

        with pytest.raises(McpPayloadError):
            parse_job_payload(["not a dict"])  # type: ignore[arg-type]


# ==============================================================================
# 5. Mock MCP Client Failure Modes & Concurrency
# ==============================================================================

class TestMockMcpClientStress:
    class CustomTestException(Exception):
        pass

    @pytest.mark.asyncio
    async def test_custom_exception_injection(self):
        """
        Verify that MockMcpJobClient raises exact custom exception instances when injected.
        """
        custom_err = self.CustomTestException("Injected custom failure")

        mock_connect = MockMcpJobClient(fail_on_connect=custom_err)
        with pytest.raises(self.CustomTestException, match="Injected custom failure"):
            await mock_connect.connect()

        mock_fetch = MockMcpJobClient(
            jobs=[{"title": "T", "company": "C", "description": "D"}],
            fail_on_fetch=custom_err,
        )
        await mock_fetch.connect()
        with pytest.raises(self.CustomTestException, match="Injected custom failure"):
            await mock_fetch.fetch_jobs(limit=1)

    @pytest.mark.asyncio
    async def test_timeout_on_fetch_inherits_standard_timeout(self):
        """
        Verify timeout_on_fetch raises standard TimeoutError.
        """
        mock = MockMcpJobClient(
            jobs=[{"title": "T", "company": "C", "description": "D"}],
            timeout_on_fetch=True,
        )
        await mock.connect()
        with pytest.raises(TimeoutError):
            await mock.fetch_jobs(limit=1)

    @pytest.mark.asyncio
    async def test_error_after_zero_calls(self):
        """
        Verify error_after_n_calls=0 trips immediately on the very first call.
        """
        mock = MockMcpJobClient(
            jobs=[{"title": "T", "company": "C", "description": "D"}],
            error_after_n_calls=0,
        )
        await mock.connect()
        with pytest.raises(McpToolExecutionError, match="error after 0 calls"):
            await mock.fetch_jobs(limit=1)

    @pytest.mark.asyncio
    async def test_pagination_boundary_limits(self):
        """
        Verify pagination edge cases: limit=0, negative limits, and empty fixtures.
        """
        mock = MockMcpJobClient(
            jobs=[{"title": "T", "company": "C", "description": "D"}],
            mode="loop",
        )
        await mock.connect()

        assert await mock.fetch_jobs(limit=0) == []
        assert await mock.fetch_jobs(limit=-1) == []

        mock.mode = "sequential"
        assert await mock.fetch_jobs(limit=0) == []
        assert await mock.fetch_jobs(limit=-5) == []

        mock.mode = "all"
        assert await mock.fetch_jobs(limit=0) == []
        assert await mock.fetch_jobs(limit=-10) == []

    @pytest.mark.asyncio
    async def test_loop_mode_large_iteration_bounds(self):
        """
        Verify cyclic loop pagination with high iteration volume.
        """
        jobs = [
            {"title": f"Job {i}", "company": "Co", "description": f"Desc {i}"}
            for i in range(3)
        ]
        mock = MockMcpJobClient(jobs=jobs, mode="loop")
        await mock.connect()

        # Fetch 300 items across 3-item pool
        batch = await mock.fetch_jobs(limit=300)
        assert len(batch) == 300
        assert batch[0].title == "Job 0"
        assert batch[1].title == "Job 1"
        assert batch[2].title == "Job 2"
        assert batch[3].title == "Job 0"
        assert mock.cursor == 0  # 300 % 3 == 0

    @pytest.mark.asyncio
    async def test_concurrent_fetching_race_safety(self):
        """
        Verify thread-safe / coroutine-safe cursor advancement when 50 concurrent
        tasks fetch from the mock client.
        """
        jobs = [
            {"title": f"Job {i}", "company": "Co", "description": f"Desc {i}"}
            for i in range(100)
        ]
        mock = MockMcpJobClient(jobs=jobs, mode="sequential", latency_seconds=0.005)
        await mock.connect()

        tasks = [asyncio.create_task(mock.fetch_jobs(limit=1)) for _ in range(50)]
        results = await asyncio.gather(*tasks)

        fetched_titles = [r[0].title for r in results if r]
        assert len(fetched_titles) == 50
        assert len(set(fetched_titles)) == 50  # Exactly 50 distinct jobs fetched
        assert mock.cursor == 50
        assert mock.fetch_count == 50
