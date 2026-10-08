from __future__ import annotations
"""
DuckDB Database Manager and Connection Lifecycle.

Provides thread-safe, non-blocking asynchronous access to embedded DuckDB
with immediate disk checkpoints and zero large arrays in memory.
"""


import asyncio
from pathlib import Path
import threading
from typing import Any, List, Optional, Sequence, Tuple

import duckdb


# Table and Index DDL Statements
DDL_CREATE_JOB_POSTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS job_postings (
    id VARCHAR PRIMARY KEY,
    content_hash VARCHAR UNIQUE NOT NULL,
    title VARCHAR NOT NULL,
    company VARCHAR NOT NULL,
    location VARCHAR,
    raw_description VARCHAR NOT NULL,
    cleaned_description VARCHAR,
    status VARCHAR NOT NULL,
    fit_score INTEGER,
    recommendation VARCHAR,
    evaluation JSON,
    is_truncated BOOLEAN DEFAULT FALSE,
    token_count INTEGER,
    url VARCHAR,
    source VARCHAR DEFAULT 'mcp',
    error_message VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

DDL_CREATE_INDEX_HASH = """
CREATE INDEX IF NOT EXISTS idx_jobs_content_hash ON job_postings (content_hash);
"""

DDL_CREATE_INDEX_STATUS = """
CREATE INDEX IF NOT EXISTS idx_jobs_status ON job_postings (status);
"""

DDL_CREATE_INDEX_CREATED_AT = """
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON job_postings (created_at);
"""


class DatabaseManager:
    """
    Manages embedded DuckDB connection lifecycle, concurrency locks,
    immediate checkpoints, and asynchronous thread offloading.
    """

    def __init__(self, db_path: str = "open_job_loop.duckdb") -> None:
        """
        Initialize the database manager.

        Args:
            db_path: Path to the DuckDB file, or ':memory:' for transient storage.
        """
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn: Optional[duckdb.DuckDBPyConnection] = None
        self._is_memory = (db_path == ":memory:")

    def connect(self) -> duckdb.DuckDBPyConnection:
        """
        Get or initialize the underlying DuckDB connection.
        Ensures parent directories exist for file-based databases.
        """
        with self._lock:
            if self._conn is None:
                if not self._is_memory:
                    parent_dir = Path(self.db_path).resolve().parent
                    parent_dir.mkdir(parents=True, exist_ok=True)
                self._conn = duckdb.connect(self.db_path, read_only=False)
                # Configure pragmas for performance and durability
                self._conn.execute("PRAGMA threads=4;")
            return self._conn

    def initialize(self) -> None:
        """
        Synchronously initialize database tables and indexes.
        Protected by the write lock.
        """
        with self._lock:
            conn = self.connect()
            conn.execute(DDL_CREATE_JOB_POSTINGS_TABLE)
            conn.execute(DDL_CREATE_INDEX_HASH)
            conn.execute(DDL_CREATE_INDEX_STATUS)
            conn.execute(DDL_CREATE_INDEX_CREATED_AT)
            conn.execute("CHECKPOINT;")

    def execute_write(
        self,
        query: str,
        params: Sequence[Any] = (),
        checkpoint: bool = True
    ) -> List[Tuple[Any, ...]]:
        """
        Execute a mutating SQL statement (INSERT, UPDATE, DELETE) under the write lock,
        optionally flushing WAL to disk immediately via CHECKPOINT.

        Args:
            query: SQL statement with parameter placeholders (?).
            params: Sequence of parameter values.
            checkpoint: Whether to trigger an immediate WAL checkpoint.

        Returns:
            List of result rows if RETURNING clause was used, else empty list.
        """
        with self._lock:
            conn = self.connect()
            cursor = conn.cursor()
            try:
                cursor.execute(query, params)
                results = cursor.fetchall() if cursor.description else []
                if checkpoint:
                    conn.execute("CHECKPOINT;")
                return results
            finally:
                cursor.close()

    def execute_read(
        self,
        query: str,
        params: Sequence[Any] = ()
    ) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        """
        Execute a read-only SQL query without write locks.

        Args:
            query: SQL query with parameter placeholders (?).
            params: Sequence of parameter values.

        Returns:
            Tuple of (column_names, rows).
        """
        conn = self.connect()
        cursor = conn.cursor()
        try:
            cursor.execute(query, params)
            column_names = [col[0] for col in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
            return column_names, rows
        finally:
            cursor.close()

    def execute_scalar(self, query: str, params: Sequence[Any] = ()) -> Any:
        """
        Execute a query expecting a single scalar return value.

        Args:
            query: SQL query.
            params: Sequence of parameter values.

        Returns:
            Scalar result or None.
        """
        conn = self.connect()
        cursor = conn.cursor()
        try:
            cursor.execute(query, params)
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            cursor.close()

    def checkpoint(self) -> None:
        """Explicitly flush all dirty pages and WAL to disk."""
        with self._lock:
            if self._conn is not None:
                self._conn.execute("CHECKPOINT;")

    def close(self) -> None:
        """Safely close the DuckDB connection."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("CHECKPOINT;")
                except Exception:
                    pass
                self._conn.close()
                self._conn = None

    # Asynchronous Thread Offloading Methods

    async def ainitialize(self) -> None:
        """Asynchronously initialize the database schema."""
        await asyncio.to_thread(self.initialize)

    async def aexecute_write(
        self,
        query: str,
        params: Sequence[Any] = (),
        checkpoint: bool = True
    ) -> List[Tuple[Any, ...]]:
        """Asynchronously execute a write operation on a worker thread."""
        return await asyncio.to_thread(self.execute_write, query, params, checkpoint)

    async def aexecute_read(
        self,
        query: str,
        params: Sequence[Any] = ()
    ) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        """Asynchronously execute a read query on a worker thread."""
        return await asyncio.to_thread(self.execute_read, query, params)

    async def aexecute_scalar(self, query: str, params: Sequence[Any] = ()) -> Any:
        """Asynchronously execute a scalar query on a worker thread."""
        return await asyncio.to_thread(self.execute_scalar, query, params)

    async def acheckpoint(self) -> None:
        """Asynchronously checkpoint the database."""
        await asyncio.to_thread(self.checkpoint)

    async def aclose(self) -> None:
        """Asynchronously close the database connection."""
        await asyncio.to_thread(self.close)

    # Context Manager Protocols

    async def __aenter__(self) -> DatabaseManager:
        await self.ainitialize()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
