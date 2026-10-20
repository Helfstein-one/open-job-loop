import http.server
import socketserver
import threading

import duckdb
import pytest

from src.application.use_cases.auto_apply import AutoApplyWorker
from src.application.use_cases.profile import ProfileManager

MOCK_HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
    <title>Mock LinkedIn Job Easy Apply</title>
</head>
<body>
    <h1>Staff Data Engineer - FinTech Labs</h1>
    <button id="easy-apply-btn" onclick="openModal()">Easy Apply</button>

    <div id="modal" style="display: none; border: 1px solid black; padding: 20px;">
        <h2>Easy Apply Application</h2>
        <form id="apply-form">
            <div>
                <label for="py_exp" class="artdeco-text-input--label">How many years of experience do you have with Python?</label>
                <input id="py_exp" type="text" value="" />
            </div>
            <div>
                <label for="salary_exp" class="artdeco-text-input--label">What is your desired salary or compensation expectations?</label>
                <input id="salary_exp" type="text" value="" />
            </div>
            <div>
                <label for="sponsor_exp" class="artdeco-text-input--label">Will you now or in the future require visa sponsorship to work in the United States?</label>
                <input id="sponsor_exp" type="text" value="" />
            </div>
            <br/>
            <button id="submit-btn" type="button" onclick="submitApp()">Submit application</button>
        </form>
    </div>

    <script>
        function openModal() {
            document.getElementById('modal').style.display = 'block';
        }
        function submitApp() {
            document.body.innerHTML += '<h3>Application Submitted</h3>';
        }
    </script>
</body>
</html>
"""


class LocalMockHttpServer:
    def __init__(self, html_content: str):
        self.html_content = html_content
        self.port = 0
        self.httpd = None
        self.thread = None

    def start(self):
        content = self.html_content.encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, format, *args):
                pass  # Silence server access logs

        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


@pytest.fixture
def mock_job_server():
    server = LocalMockHttpServer(MOCK_HTML_PAGE)
    server.start()
    yield f"http://127.0.0.1:{server.port}"
    server.stop()


SAMPLE_E2E_PROFILE_YAML = """
candidate:
  name: "Maurício Helfstein Gonçalves"
  email: "mauricio@example.com"
  phone: "+55 11 99999-9999"
  location: "São Paulo, Brazil"
  title: "Staff Data Engineer"
  years_of_experience: 10
  linkedin_url: "https://linkedin.com/in/mauricio"

work_authorization:
  authorized_in_brazil: true
  authorized_in_us: false
  requires_sponsorship_us: true
  requires_sponsorship_eu: false

preferences:
  expected_salary_usd: "$140,000"
  expected_salary_brl: "R$ 38.000"
  notice_period: "2 weeks"
  work_preference: "Remote"

summary: "Staff Data Engineer with 10+ years experience in Python, DuckDB, and Spark."

skills:
  languages: ["Python", "SQL"]
  data_engineering: ["DuckDB", "Spark"]

standard_answers:
  - question: "How many years of experience do you have with Python?"
    answer: "10"
  - question: "What is your desired salary or compensation expectations?"
    answer: "$140,000"
  - question: "Will you now or in the future require visa sponsorship to work in the United States?"
    answer: "Yes"
  - question: "Will you now or in the future require visa sponsorship?"
    answer: "Yes"
"""


@pytest.mark.asyncio
async def test_e2e_linkedin_pattern_identification_and_auto_apply(tmp_path, mock_job_server):
    db_file = str(tmp_path / "e2e_apply.duckdb")
    yaml_file = str(tmp_path / "test_profile.yaml")
    with open(yaml_file, "w", encoding="utf-8") as f:
        f.write(SAMPLE_E2E_PROFILE_YAML)

    # 1. Sync candidate profile into DuckDB VSS
    profile_manager = ProfileManager(db_path=db_file, mock_mode=True)
    count = await profile_manager.sync_from_yaml(yaml_file)
    assert count >= 5

    # 2. Verify pattern identification for form questions
    py_ans = await profile_manager.synthesize_answer(
        "How many years of experience do you have with Python?"
    )
    assert "10" in py_ans

    salary_ans = await profile_manager.synthesize_answer(
        "What is your desired salary or compensation expectations?"
    )
    assert "140,000" in salary_ans or "38.000" in salary_ans

    sponsorship_ans = await profile_manager.synthesize_answer(
        "Will you now or in the future require visa sponsorship?"
    )
    assert "Yes" in sponsorship_ans

    # 3. Insert SHORTLISTED job pointing to local mock server
    conn = duckdb.connect(db_file)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_postings (
            id VARCHAR PRIMARY KEY,
            title VARCHAR NOT NULL,
            company VARCHAR NOT NULL,
            location VARCHAR,
            url VARCHAR NOT NULL,
            raw_description TEXT NOT NULL,
            status VARCHAR NOT NULL DEFAULT 'SHORTLISTED',
            content_hash VARCHAR UNIQUE,
            rationale TEXT,
            matched_skills VARCHAR,
            missing_skills VARCHAR,
            created_at TIMESTAMP
        );
    """)
    conn.execute(
        "INSERT INTO job_postings (id, title, company, location, url, raw_description, status, content_hash, rationale, matched_skills, missing_skills) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "job-e2e-123",
            "Staff Data Engineer",
            "FinTech Labs",
            "Remote",
            mock_job_server,
            "Staff Data Engineer job description requiring Python and Spark.",
            "SHORTLISTED",
            "hash-12345",
            "Strong match on distributed systems and Python",
            "Python, Spark, DuckDB",
            "",
        ),
    )
    conn.close()

    # 4. Execute AutoApplyWorker in dry-run mode
    worker = AutoApplyWorker(
        db_path=db_file,
        headless=True,
        dry_run=True,
        mock_mode=True,
    )
    stats = await worker.run()

    # 5. Assertions
    assert stats["processed"] == 1
    assert stats["applied"] == 1
    assert stats["failed"] == 0

    # Check updated database record
    conn = duckdb.connect(db_file)
    updated_job = conn.execute(
        "SELECT status FROM job_postings WHERE id = 'job-e2e-123'"
    ).fetchone()
    conn.close()

    assert updated_job is not None
    assert updated_job[0] == "APPLIED_DRY_RUN"
