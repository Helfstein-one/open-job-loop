from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import async_playwright

from src.application.use_cases.active_learning import ActiveLearningService
from src.domain.models import ApplicationChannel
from src.infrastructure.adapters.ats.ashby import AshbyAdapter
from src.infrastructure.adapters.ats.greenhouse import GreenhouseAdapter
from src.infrastructure.adapters.ats.lever import LeverAdapter


@pytest.fixture
def mock_active_learning():
    mock_pm = MagicMock()
    mock_pm.synthesize_answer_with_confidence = AsyncMock(return_value=("Candidate Answer", 0.90))
    mock_pm.add_chunk = AsyncMock()

    service = ActiveLearningService(
        profile_manager=mock_pm,
        min_confidence=0.70,
        interactive=False,
    )
    return service


@pytest.fixture
def dummy_resume_file(tmp_path: Path):
    resume = tmp_path / "resume.pdf"
    resume.write_text("%PDF-1.4 dummy pdf content")
    return str(resume)


@pytest.mark.asyncio
async def test_greenhouse_form_fill_dry_run(
    mock_active_learning, dummy_resume_file, tmp_path: Path
):
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>Greenhouse Test Form</title></head>
    <body>
        <div id="application_form">
            <input id="first_name" type="text" />
            <input id="last_name" type="text" />
            <input id="email" type="email" />
            <input id="phone" type="tel" />
            <input id="resume" type="file" />
            <div class="field">
                <label>Years of experience with Python</label>
                <input type="text" />
            </div>
            <button id="submit_app" type="button">Submit Application</button>
        </div>
    </body>
    </html>
    """
    html_file = tmp_path / "greenhouse_form.html"
    html_file.write_text(html, encoding="utf-8")
    file_url = f"file://{html_file.resolve()}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        adapter = GreenhouseAdapter()
        job_ctx = {"id": "job-gh-1", "title": "Staff Engineer", "company": "Stripe"}

        # Test dry-run
        res = await adapter.fill_and_submit(
            page=page,
            url=file_url,
            job_context=job_ctx,
            active_learning=mock_active_learning,
            resume_path=dummy_resume_file,
            dry_run=True,
        )

        assert res.success is True
        assert res.channel == ApplicationChannel.GREENHOUSE
        assert res.dry_run is True
        assert res.fields_filled >= 4

        # Verify fields on page were populated
        assert await page.locator("#first_name").input_value() != ""
        assert await page.locator("#email").input_value() != ""
        assert await page.locator("#phone").input_value() != ""

        await browser.close()


@pytest.mark.asyncio
async def test_lever_form_fill_dry_run(mock_active_learning, dummy_resume_file, tmp_path: Path):
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>Lever Test Form</title></head>
    <body>
        <form id="application-form">
            <input name="name" type="text" />
            <input name="email" type="email" />
            <input name="phone" type="tel" />
            <input name="org" type="text" />
            <input name="urls[LinkedIn]" type="text" />
            <input name="resume" type="file" />
            <div class="application-question">
                <div class="text">Why do you want to work at Netflix?</div>
                <textarea></textarea>
            </div>
            <button id="btn-submit" type="button">Submit application</button>
        </form>
    </body>
    </html>
    """
    html_file = tmp_path / "lever_form.html"
    html_file.write_text(html, encoding="utf-8")
    file_url = f"file://{html_file.resolve()}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        adapter = LeverAdapter()
        job_ctx = {"id": "job-lever-1", "title": "Staff Backend Engineer", "company": "Netflix"}

        res = await adapter.fill_and_submit(
            page=page,
            url=file_url,
            job_context=job_ctx,
            active_learning=mock_active_learning,
            resume_path=dummy_resume_file,
            dry_run=True,
        )

        assert res.success is True
        assert res.channel == ApplicationChannel.LEVER
        assert res.dry_run is True
        assert res.fields_filled >= 4
        assert await page.locator("input[name='name']").input_value() != ""
        assert await page.locator("input[name='email']").input_value() != ""

        await browser.close()


@pytest.mark.asyncio
async def test_ashby_form_fill_dry_run(mock_active_learning, dummy_resume_file, tmp_path: Path):
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>Ashby Test Form</title></head>
    <body>
        <form>
            <input name="name" type="text" />
            <input name="email" type="email" />
            <input name="phone" type="tel" />
            <input type="file" />
            <div data-ashby-field="true">
                <label>Tell us about your architectural experience</label>
                <textarea></textarea>
            </div>
            <button type="submit">Submit Application</button>
        </form>
    </body>
    </html>
    """
    html_file = tmp_path / "ashby_form.html"
    html_file.write_text(html, encoding="utf-8")
    file_url = f"file://{html_file.resolve()}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        adapter = AshbyAdapter()
        job_ctx = {"id": "job-ashby-1", "title": "Lead Engineer", "company": "Ramp"}

        res = await adapter.fill_and_submit(
            page=page,
            url=file_url,
            job_context=job_ctx,
            active_learning=mock_active_learning,
            resume_path=dummy_resume_file,
            dry_run=True,
        )

        assert res.success is True
        assert res.channel == ApplicationChannel.ASHBY
        assert res.dry_run is True
        assert res.fields_filled >= 3
        assert await page.locator("input[name='name']").input_value() != ""
        assert await page.locator("input[name='email']").input_value() != ""

        await browser.close()
