from pathlib import Path
from unittest.mock import AsyncMock, patch

import duckdb
import pytest

from src.application.use_cases.auto_apply import AutoApplyWorker
from src.domain.models import ApplicationChannel
from src.infrastructure.adapters.ats.base import ApplicationResult


@pytest.mark.asyncio
async def test_e2e_multi_channel_auto_apply_worker(tmp_path: Path):
    db_file = str(tmp_path / "multi_channel_test.duckdb")
    resume_file = tmp_path / "resume.pdf"
    resume_file.write_text("%PDF-1.4 dummy resume")

    # Setup DuckDB database with shortlisted postings across various channels
    conn = duckdb.connect(db_file)
    conn.execute("""
        CREATE TABLE job_postings (
            id VARCHAR PRIMARY KEY,
            title VARCHAR NOT NULL,
            company VARCHAR NOT NULL,
            location VARCHAR,
            url VARCHAR NOT NULL,
            raw_description TEXT NOT NULL,
            truncated_description TEXT,
            content_hash VARCHAR(64) UNIQUE NOT NULL,
            status VARCHAR NOT NULL DEFAULT 'SHORTLISTED',
            fit_score INTEGER,
            recommendation VARCHAR,
            rationale TEXT,
            matched_skills VARCHAR,
            missing_skills VARCHAR,
            error_message TEXT,
            retry_count INTEGER DEFAULT 0,
            channel VARCHAR,
            application_log TEXT,
            ingested_at TIMESTAMP,
            triaged_at TIMESTAMP,
            created_at TIMESTAMP
        );
    """)

    conn.execute("""
        INSERT INTO job_postings (id, title, company, url, raw_description, content_hash, status)
        VALUES 
            ('job-1', 'Staff Backend Engineer', 'Stripe', 'https://boards.greenhouse.io/stripe/jobs/101', 'Desc', 'hash1', 'SHORTLISTED'),
            ('job-2', 'Principal Python Architect', 'Netflix', 'https://jobs.lever.co/netflix/202', 'Desc', 'hash2', 'SHORTLISTED'),
            ('job-3', 'Lead Infrastructure Engineer', 'Ramp', 'https://jobs.ashbyhq.com/ramp/303', 'Desc', 'hash3', 'SHORTLISTED');
    """)
    conn.execute("CHECKPOINT;")
    conn.close()

    # Mock ATS adapter executions to simulate realistic success
    mock_gh_res = ApplicationResult(
        success=True,
        channel=ApplicationChannel.GREENHOUSE,
        job_id="job-1",
        fields_filled=5,
        logs=["Greenhouse dry-run completed"],
        dry_run=True,
    )
    mock_lever_res = ApplicationResult(
        success=True,
        channel=ApplicationChannel.LEVER,
        job_id="job-2",
        fields_filled=6,
        logs=["Lever dry-run completed"],
        dry_run=True,
    )
    mock_ashby_res = ApplicationResult(
        success=True,
        channel=ApplicationChannel.ASHBY,
        job_id="job-3",
        fields_filled=4,
        logs=["Ashby dry-run completed"],
        dry_run=True,
    )

    with (
        patch(
            "src.infrastructure.adapters.ats.greenhouse.GreenhouseAdapter.fill_and_submit",
            AsyncMock(return_value=mock_gh_res),
        ),
        patch(
            "src.infrastructure.adapters.ats.lever.LeverAdapter.fill_and_submit",
            AsyncMock(return_value=mock_lever_res),
        ),
        patch(
            "src.infrastructure.adapters.ats.ashby.AshbyAdapter.fill_and_submit",
            AsyncMock(return_value=mock_ashby_res),
        ),
    ):

        worker = AutoApplyWorker(
            db_path=db_file,
            headless=True,
            dry_run=True,
            mock_mode=True,
            resume_path=str(resume_file),
            interactive_active_learning=False,
        )

        stats = await worker.run(limit=3)

        assert stats["processed"] == 3
        assert stats["applied"] == 3
        assert stats["failed"] == 0

    # Verify DuckDB records have been updated with channel and logs
    conn = duckdb.connect(db_file)
    rows = conn.execute(
        "SELECT id, status, channel, application_log FROM job_postings ORDER BY id"
    ).fetchall()
    conn.close()

    assert len(rows) == 3
    assert rows[0][0] == "job-1"
    assert rows[0][1] == "APPLIED_DRY_RUN"
    assert rows[0][2] == "GREENHOUSE"
    assert "Greenhouse dry-run completed" in rows[0][3]

    assert rows[1][0] == "job-2"
    assert rows[1][1] == "APPLIED_DRY_RUN"
    assert rows[1][2] == "LEVER"
    assert "Lever dry-run completed" in rows[1][3]

    assert rows[2][0] == "job-3"
    assert rows[2][1] == "APPLIED_DRY_RUN"
    assert rows[2][2] == "ASHBY"
    assert "Ashby dry-run completed" in rows[2][3]
