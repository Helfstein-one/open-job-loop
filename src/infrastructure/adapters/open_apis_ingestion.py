from __future__ import annotations

"""
Open APIs Job Ingestion Adapter for open-job-loop.

Implements JobIngestionPort to discover remote jobs across the US, UK, and European Union
from public developer job APIs (Jobicy, Arbeitnow, Remotive) with zero login or rate-limit barriers.
"""

import asyncio
import html
import logging
import re
from typing import Any, List, Optional

import httpx

from src.application.ports.job_ingestion import JobIngestionPort
from src.domain.models import JobPosting, JobStatus
from src.infrastructure.adapters.repository import compute_job_hash

logger = logging.getLogger(__name__)


def clean_html_text(text: str) -> str:
    """Strip HTML tags and unescape HTML entities into clean markdown/prose."""
    if not text:
        return ""
    # Replace block level elements with newlines
    text = re.sub(r"<(?:p|div|br|h[1-6]|li)[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Strip all remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Unescape HTML entities (&amp;, &lt;, etc.)
    text = html.unescape(text)
    # Normalize multiple whitespaces
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


class OpenApisJobClient(JobIngestionPort):
    """
    Unified Ingestion Adapter querying public remote job APIs (Jobicy, Arbeitnow, Remotive).
    Complies with Hexagonal Architecture via JobIngestionPort.
    """

    def __init__(
        self,
        keywords: Optional[str] = "Python",
        location: Optional[str] = "United States, United Kingdom, European Union",
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 15.0,
    ) -> None:
        self.keywords = keywords or "Python"
        self.location = location or "Remote"
        self._external_client = http_client is not None
        self._client = http_client
        self.timeout = timeout
        self._connected = False

    async def connect(self) -> None:
        """Establish HTTP client session."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "application/json, text/plain, */*",
                },
                follow_redirects=True,
            )
        self._connected = True
        logger.info("OpenApisJobClient session established.")

    async def disconnect(self) -> None:
        """Close HTTP client session."""
        if self._client and not self._external_client and not self._client.is_closed:
            await self._client.aclose()
        self._connected = False
        logger.info("OpenApisJobClient session terminated.")

    async def fetch_jobs(self, limit: int = 100, **kwargs: Any) -> List[JobPosting]:
        """
        Fetch and normalize jobs across Jobicy, Arbeitnow, and Remotive up to `limit`.
        """
        if not self._connected or not self._client or self._client.is_closed:
            await self.connect()

        kw = kwargs.get("keywords") or self.keywords
        loc = kwargs.get("location") or self.location

        logger.info(
            "Fetching open jobs from public APIs (keywords='%s', location='%s', target=%d)",
            kw,
            loc,
            limit,
        )

        # Run concurrent fetch from all 3 public providers
        results = await asyncio.gather(
            self._fetch_jobicy(limit=limit, keywords=kw),
            self._fetch_arbeitnow(limit=limit, keywords=kw),
            self._fetch_remotive(limit=limit, keywords=kw),
            return_exceptions=True,
        )

        all_postings: List[JobPosting] = []
        seen_hashes: set[str] = set()

        for idx, res in enumerate(results):
            provider_name = ["Jobicy", "Arbeitnow", "Remotive"][idx]
            if isinstance(res, Exception):
                logger.warning("Provider %s fetch failed: %s", provider_name, res)
                continue
            if isinstance(res, list):
                logger.info("Provider %s returned %d candidates", provider_name, len(res))
                for posting in res:
                    if posting.content_hash not in seen_hashes:
                        seen_hashes.add(posting.content_hash)
                        all_postings.append(posting)

        logger.info("Total unique open jobs fetched across public sources: %d", len(all_postings))
        return all_postings[:limit]

    async def _fetch_jobicy(self, limit: int, keywords: str) -> List[JobPosting]:
        """Fetch remote jobs from Jobicy public API."""
        assert self._client is not None
        postings: List[JobPosting] = []
        # Jobicy supports geo filters like usa, uk, emea
        geos = ["usa", "uk", "emea"]
        for geo in geos:
            if len(postings) >= limit:
                break
            try:
                url = f"https://jobicy.com/api/v2/remote-jobs?count=50&geo={geo}"
                resp = await self._client.get(url)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                raw_jobs = data.get("jobs", [])
                for item in raw_jobs:
                    title = item.get("jobTitle", "").strip()
                    desc = clean_html_text(
                        item.get("jobDescription") or item.get("jobExcerpt") or ""
                    )
                    company = item.get("companyName", "").strip() or "Unknown Company"
                    job_url = item.get("url", "")
                    job_geo = item.get("jobGeo", geo).upper()

                    # Filter by keywords if provided
                    if keywords:
                        kw_terms = [t.lower() for t in keywords.split() if len(t) > 2]
                        full_content = f"{title} {desc}".lower()
                        if not any(t in full_content for t in kw_terms):
                            continue

                    chash = compute_job_hash(raw_description=desc, title=title, company=company)
                    posting = JobPosting(
                        content_hash=chash,
                        title=title,
                        company=company,
                        location=f"Remote ({job_geo})",
                        raw_description=desc,
                        cleaned_description=desc,
                        description=desc,
                        url=job_url,
                        status=JobStatus.INGESTED,
                    )
                    postings.append(posting)
                    if len(postings) >= limit:
                        break
            except Exception as e:
                logger.debug("Jobicy geo=%s error: %s", geo, e)
        return postings

    async def _fetch_arbeitnow(self, limit: int, keywords: str) -> List[JobPosting]:
        """Fetch European & Remote jobs from Arbeitnow public API."""
        assert self._client is not None
        postings: List[JobPosting] = []
        try:
            url = "https://www.arbeitnow.com/api/job-board-api"
            resp = await self._client.get(url)
            if resp.status_code != 200:
                return postings
            data = resp.json()
            raw_jobs = data.get("data", [])
            for item in raw_jobs:
                title = item.get("title", "").strip()
                desc = clean_html_text(item.get("description", ""))
                company = item.get("company_name", "").strip() or "Unknown Company"
                job_url = item.get("url", "")
                location = item.get("location", "Remote (EU/Global)")

                if keywords:
                    kw_terms = [t.lower() for t in keywords.split() if len(t) > 2]
                    full_content = f"{title} {desc}".lower()
                    if not any(t in full_content for t in kw_terms):
                        continue

                chash = compute_job_hash(raw_description=desc, title=title, company=company)
                posting = JobPosting(
                    content_hash=chash,
                    title=title,
                    company=company,
                    location=location,
                    raw_description=desc,
                    cleaned_description=desc,
                    description=desc,
                    url=job_url,
                    status=JobStatus.INGESTED,
                )
                postings.append(posting)
                if len(postings) >= limit:
                    break
        except Exception as e:
            logger.debug("Arbeitnow error: %s", e)
        return postings

    async def _fetch_remotive(self, limit: int, keywords: str) -> List[JobPosting]:
        """Fetch software jobs from Remotive public API."""
        assert self._client is not None
        postings: List[JobPosting] = []
        try:
            url = "https://remotive.com/api/remote-jobs?category=software-dev&limit=100"
            resp = await self._client.get(url)
            if resp.status_code != 200:
                return postings
            data = resp.json()
            raw_jobs = data.get("jobs", [])
            for item in raw_jobs:
                title = item.get("title", "").strip()
                desc = clean_html_text(item.get("description", ""))
                company = item.get("company_name", "").strip() or "Unknown Company"
                job_url = item.get("url", "")
                candidate_loc = item.get("candidate_required_location") or "Remote"

                if keywords:
                    kw_terms = [t.lower() for t in keywords.split() if len(t) > 2]
                    full_content = f"{title} {desc}".lower()
                    if not any(t in full_content for t in kw_terms):
                        continue

                chash = compute_job_hash(raw_description=desc, title=title, company=company)
                posting = JobPosting(
                    content_hash=chash,
                    title=title,
                    company=company,
                    location=f"Remote ({candidate_loc})",
                    raw_description=desc,
                    cleaned_description=desc,
                    description=desc,
                    url=job_url,
                    status=JobStatus.INGESTED,
                )
                postings.append(posting)
                if len(postings) >= limit:
                    break
        except Exception as e:
            logger.debug("Remotive error: %s", e)
        return postings


class HybridJobClient(JobIngestionPort):
    """
    Hybrid Ingestion Port adapter combining Open Public APIs (Jobicy, Arbeitnow, Remotive)
    with LinkedIn MCP client, with automatic anti-bot fault isolation.
    """

    def __init__(
        self,
        open_client: OpenApisJobClient,
        linkedin_client: Optional[JobIngestionPort] = None,
        enable_linkedin: bool = True,
    ) -> None:
        self.open_client = open_client
        self.linkedin_client = linkedin_client
        self.enable_linkedin = enable_linkedin
        self._linkedin_blocked = False

    async def connect(self) -> None:
        await self.open_client.connect()
        if self.enable_linkedin and self.linkedin_client and not self._linkedin_blocked:
            try:
                await self.linkedin_client.connect()
            except Exception as e:
                logger.warning("LinkedIn MCP connect bypassed due to error: %s", e)
                self._linkedin_blocked = True

    async def disconnect(self) -> None:
        await self.open_client.disconnect()
        if self.linkedin_client:
            try:
                await self.linkedin_client.disconnect()
            except Exception:
                pass

    async def fetch_jobs(self, limit: int = 100, **kwargs: Any) -> List[JobPosting]:
        all_jobs: List[JobPosting] = []
        seen_hashes: set[str] = set()

        # 1. Fetch from Open APIs (Massive Discovery: zero risk of blocking)
        try:
            open_jobs = await self.open_client.fetch_jobs(limit=limit, **kwargs)
            for j in open_jobs:
                if j.content_hash not in seen_hashes:
                    seen_hashes.add(j.content_hash)
                    all_jobs.append(j)
            logger.info("HybridClient: Open APIs returned %d jobs", len(open_jobs))
        except Exception as e:
            logger.warning("HybridClient: Open APIs fetch error: %s", e)

        # 2. Fetch from LinkedIn MCP (Surgical Query: bounded and protected)
        if self.enable_linkedin and self.linkedin_client and not self._linkedin_blocked:
            try:
                # Limit LinkedIn query size to avoid triggering aggressive bot counters
                safe_li_limit = min(limit, 25)
                li_jobs = await self.linkedin_client.fetch_jobs(limit=safe_li_limit, **kwargs)
                for j in li_jobs:
                    if j.content_hash not in seen_hashes:
                        seen_hashes.add(j.content_hash)
                        all_jobs.append(j)
                logger.info("HybridClient: LinkedIn returned %d jobs", len(li_jobs))
            except Exception as e:
                err_msg = str(e).lower()
                if any(
                    x in err_msg
                    for x in ["auth", "login", "barrier", "blocker", "stale", "checkpoint"]
                ):
                    logger.warning(
                        "🚨 LinkedIn anti-bot / auth checkpoint triggered (%s). Isolando LinkedIn e continuando com Open APIs.",
                        e,
                    )
                    self._linkedin_blocked = True
                else:
                    logger.warning(
                        "HybridClient: LinkedIn query failed (%s). Continuando com Open APIs.", e
                    )

        return all_jobs[:limit]
