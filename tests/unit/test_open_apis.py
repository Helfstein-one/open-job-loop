from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.models import JobPosting, JobStatus
from src.infrastructure.adapters.open_apis_ingestion import (
    HybridJobClient,
    OpenApisJobClient,
    clean_html_text,
)


def test_clean_html_text():
    html_raw = "<h3>Senior Python Developer</h3><p>We are hiring! &amp; offering remote.</p><br><ul><li>FastAPI</li></ul>"
    cleaned = clean_html_text(html_raw)
    assert "Senior Python Developer" in cleaned
    assert "We are hiring! & offering remote." in cleaned
    assert "FastAPI" in cleaned
    assert "<p>" not in cleaned
    assert "&amp;" not in cleaned


@pytest.mark.asyncio
async def test_open_apis_job_client_fetch_mocked():
    client = OpenApisJobClient(keywords="Python", location="Remote")

    mock_jobicy_data = {
        "jobs": [
            {
                "jobTitle": "Senior Python Backend Engineer",
                "companyName": "Tech Corp",
                "jobDescription": "<p>Build high-scale distributed systems with Python and FastAPI.</p>",
                "url": "https://jobicy.com/job/123",
                "jobGeo": "USA",
            }
        ]
    }

    mock_arbeitnow_data = {
        "data": [
            {
                "title": "Staff Python Engineer",
                "company_name": "Euro AI",
                "description": "<p>Deep learning & Python backend APIs.</p>",
                "url": "https://arbeitnow.com/job/456",
                "location": "European Union (Remote)",
            }
        ]
    }

    mock_remotive_data = {
        "jobs": [
            {
                "title": "Python Data Platform Engineer",
                "company_name": "Cloud Data",
                "description": "<p>Asyncio and Python data pipelines.</p>",
                "url": "https://remotive.com/job/789",
                "candidate_required_location": "Worldwide",
            }
        ]
    }

    async def mock_get(url, *args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        if "jobicy" in url:
            mock_resp.json.return_value = mock_jobicy_data
        elif "arbeitnow" in url:
            mock_resp.json.return_value = mock_arbeitnow_data
        elif "remotive" in url:
            mock_resp.json.return_value = mock_remotive_data
        return mock_resp

    mock_http_client = AsyncMock()
    mock_http_client.get = mock_get
    mock_http_client.is_closed = False

    client._client = mock_http_client
    client._connected = True

    jobs = await client.fetch_jobs(limit=10)
    assert len(jobs) >= 3
    titles = [j.title for j in jobs]
    assert "Senior Python Backend Engineer" in titles
    assert "Staff Python Engineer" in titles
    assert "Python Data Platform Engineer" in titles
    for j in jobs:
        assert isinstance(j, JobPosting)
        assert j.content_hash is not None
        assert j.status == JobStatus.INGESTED


@pytest.mark.asyncio
async def test_hybrid_client_fault_isolation():
    mock_open = AsyncMock(spec=OpenApisJobClient)
    mock_open.fetch_jobs.return_value = [
        JobPosting(
            content_hash="hash1",
            title="Open Source Python Role",
            company="OpenCo",
            raw_description="Python dev needed",
            url="https://example.com/job1",
        )
    ]

    mock_linkedin = AsyncMock()
    mock_linkedin.fetch_jobs.side_effect = Exception(
        "Authentication barrier / login redirect detected"
    )

    hybrid = HybridJobClient(
        open_client=mock_open, linkedin_client=mock_linkedin, enable_linkedin=True
    )
    await hybrid.connect()

    jobs = await hybrid.fetch_jobs(limit=10)
    assert len(jobs) == 1
    assert jobs[0].title == "Open Source Python Role"
    # Verify LinkedIn was marked as blocked and isolated
    assert hybrid._linkedin_blocked is True
