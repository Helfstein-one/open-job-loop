import pytest
from src.domain.models import JobPosting

@pytest.fixture
def sample_job_posting():
    return JobPosting(
        title="Software Engineer",
        company="TechCorp",
        location="Remote",
        raw_description="Looking for a Python dev.",
    )
