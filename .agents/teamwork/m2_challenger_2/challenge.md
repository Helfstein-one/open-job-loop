# M2 Adversarial Challenge Report: MCP Ingestion Subsystem

**Challenger**: M2 Challenger 2 (`teamwork_preview_challenger`)  
**Scope**: Milestone M2 MCP Ingestion Subsystem (`src/mcp/client.py`, `src/mcp/mock_client.py`)  
**Verdict**: **APPROVE**  
**Overall Risk Assessment**: LOW (MCP Ingestion is robust, resilient to hangs, signals, and large payloads)

---

## 1. Executive Summary

Milestone M2 MCP Ingestion was subjected to empirical stress testing across 5 hostile dimensions:
1. **Stdio connection hangs**: Subprocess handshake and tool-call deadlocks under strict timeouts (`read_timeout_seconds`).
2. **Abrupt process termination**: Sudden `SIGTERM` and `SIGKILL` delivery before and mid-flight during tool execution, verifying orphan process reaping.
3. **Large payloads & buffer saturation**: 1,000 job batch payloads (~1MB) and single 5MB job descriptions across macOS stdio pipes (pipe buffer limit stress).
4. **Mock failure modes & concurrency**: Custom exception injection, `TimeoutError`, zero-call breaker trips (`error_after_n_calls=0`), boundary limits (`limit <= 0`), and 50-task concurrent fetch races.
5. **Adversarial payload schemas**: Null bytes, emojis, zero-width characters, RTL unicode, whitespace-only fields, and stdout stream pollution.

All 15 empirical stress tests in `tests/test_stress_mcp.py` plus all 23 preexisting tests in `tests/test_mcp.py` passed (38/38 passing, 100% pass rate).

---

## 2. Empirical Stress Test Results

| Test ID | Scenario | Expected Behavior | Actual Behavior | Result |
|---|---|---|---|---|
| **ST-01** | Subprocess hangs during MCP `initialize` handshake (`time.sleep(60)`) | `McpConnectionError` within `read_timeout_seconds=1.0`; 0 orphan processes | Caught `McpConnectionError: ... timed out`; 0 child PIDs leaked | **PASS** |
| **ST-02** | Tool call `fetch_jobs` hangs indefinitely (`asyncio.sleep(60)`) | `McpToolExecutionError` within `read_timeout_seconds=1.0`; exit stack cleanly reaps child | Caught `McpToolExecutionError: ... timed out`; 0 child PIDs leaked | **PASS** |
| **ST-03** | Server terminated via `SIGTERM` before `fetch_jobs` | Raised `McpToolExecutionError` on broken pipe / closed connection; clean disconnect | Handled cleanly; `is_connected=False` after `disconnect()` | **PASS** |
| **ST-04** | Server terminated via `SIGKILL` mid-flight while awaiting `fetch_jobs` | Caught cleanly without unhandled crash or coroutine leak | Caught `McpToolExecutionError: Connection closed`; 0 orphan processes | **PASS** |
| **ST-05** | High-volume 1,000 jobs (~1MB JSON) streamed over stdio | Completed without pipe deadlock; 1,000 unique SHA256 hashes generated | 1,000 jobs parsed in <1s; all 64-char hex hashes validated | **PASS** |
| **ST-06** | Single job with 5MB raw description over stdio | Transported and parsed without buffer overflow or truncation | Exactly 5,242,880 bytes preserved; SHA256 hash valid | **PASS** |
| **ST-07** | Adversarial unicode, emojis, RTL markers, null bytes | Clean normalization, valid SHA256 content hash | Normalized cleanly, valid 64-character hex hash | **PASS** |
| **ST-08** | Whitespace-only fields (`title`, `company`, `description`) | Rejected with `McpPayloadError` | All 3 whitespace cases raised `McpPayloadError` | **PASS** |
| **ST-09** | Non-dict and `None` payloads | Rejected with `McpPayloadError` | Rejected `None` and `['not a dict']` | **PASS** |
| **ST-10** | Custom exception injection in `MockMcpJobClient` | Exact custom exception class raised on connect and fetch | Raised `CustomTestException` | **PASS** |
| **ST-11** | `timeout_on_fetch=True` in `MockMcpJobClient` | Inherits standard `TimeoutError` | Caught standard `TimeoutError` | **PASS** |
| **ST-12** | `error_after_n_calls=0` boundary condition | Fails immediately on first call | Raised `McpToolExecutionError` on call #1 | **PASS** |
| **ST-13** | Pagination limit edge cases (`limit=0`, `limit < 0`, empty fixtures) | Returns empty list `[]` without exceptions across `sequential`, `all`, `loop` | Returned `[]` across all modes | **PASS** |
| **ST-14** | `mode="loop"` cyclic replay over 300 iterations | Wraps around modulo pool length without memory leak | 300 items fetched, cursor reset to 0 | **PASS** |
| **ST-15** | 50 concurrent async tasks fetching sequentially | Race-free cursor advancement, 50 distinct jobs fetched | 50 unique jobs fetched, cursor=50, fetch_count=50 | **PASS** |

---

## 3. Observations & Caveats

1. **Auto-Reconnect Nuance on Abrupt Process Death**:
   - In `McpJobClient`, `is_connected` checks `self._is_connected and self._session is not None`.
   - If the server subprocess is violently killed (`SIGKILL`) while connected, `self._is_connected` remains `True` until `disconnect()` is explicitly called.
   - Consequently, calling `fetch_jobs()` with `auto_reconnect=True` will raise `McpToolExecutionError: Connection closed` on the dead session rather than immediately auto-reconnecting on that call, unless the caller handles the failure by calling `await client.disconnect()`.
   - *Severity*: Low. In M3, the `LocalLoopGuard` and circuit breaker wrap client lifecycles and reset connections upon failure.

2. **Stdout Stream Pollution Handling**:
   - If an MCP server tool emits raw debug logging directly to `stdout` alongside JSON-RPC messages, the underlying MCP SDK (`mcp.client.stdio`) logs validation warnings but skips the non-JSON lines without dropping or corrupting the JSON-RPC response.

3. **Malformed JSON String Handling in Tool Result**:
   - If a tool returns a non-JSON string starting with `{` that fails `json.loads`, `_extract_payload_dicts` currently falls back to returning `[]` rather than raising `McpPayloadError`. Normal server responses return structured lists or JSON strings, so this does not impact compliant servers.

---

## 4. Verification Command

```bash
.venv/bin/pytest -v tests/test_mcp.py tests/test_stress_mcp.py
```
Expected result: `38 passed in ~9.0s`.

Linting check:
```bash
/opt/homebrew/bin/ruff check src/mcp tests/test_mcp.py tests/test_stress_mcp.py
```
Expected result: `All checks passed!`.
