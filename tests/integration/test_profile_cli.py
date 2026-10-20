import pytest
from typer.testing import CliRunner

from src.presentation.cli import app

runner = CliRunner()

SAMPLE_PROFILE_CONTENT = """
candidate:
  name: "Maurício Helfstein Gonçalves"
  email: "mauricio@example.com"
  phone: "+55 11 99999-9999"
  years_of_experience: 10
skills:
  languages: ["Python"]
  frameworks: ["FastAPI", "DuckDB"]
standard_answers:
  - question: "How many years of Python experience do you have?"
    answer: "10"
"""


@pytest.fixture
def profile_yaml_file(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text(SAMPLE_PROFILE_CONTENT, encoding="utf-8")
    return str(p)


def test_cli_profile_sync_mock(tmp_path, profile_yaml_file):
    db_file = str(tmp_path / "cli_test.duckdb")
    result = runner.invoke(
        app,
        ["profile", "sync", "--file", profile_yaml_file, "--db", db_file, "--mock"],
    )
    assert result.exit_code == 0
    assert "Successfully vectorized and indexed" in result.stdout


def test_cli_profile_show(tmp_path, profile_yaml_file):
    db_file = str(tmp_path / "cli_test.duckdb")
    runner.invoke(
        app,
        ["profile", "sync", "--file", profile_yaml_file, "--db", db_file, "--mock"],
    )
    result = runner.invoke(app, ["profile", "show", "--db", db_file])
    assert result.exit_code == 0
    assert "personal_bio" in result.stdout
    assert "technical_skills" in result.stdout


def test_cli_profile_ask_pattern(tmp_path, profile_yaml_file):
    db_file = str(tmp_path / "cli_test.duckdb")
    runner.invoke(
        app,
        ["profile", "sync", "--file", profile_yaml_file, "--db", db_file, "--mock"],
    )
    result = runner.invoke(
        app,
        ["profile", "ask", "How many years of Python experience?", "--db", db_file, "--mock"],
    )
    assert result.exit_code == 0
    assert "Synthesized Answer" in result.stdout
    assert "10" in result.stdout


def test_cli_apply_empty_shortlist(tmp_path, profile_yaml_file):
    db_file = str(tmp_path / "cli_test.duckdb")
    runner.invoke(
        app,
        ["profile", "sync", "--file", profile_yaml_file, "--db", db_file, "--mock"],
    )
    result = runner.invoke(
        app,
        ["apply", "--db", db_file, "--dry-run", "--mock"],
    )
    assert result.exit_code == 0
    assert "Auto-Apply Execution Finished" in result.stdout
    assert "Processed: 0" in result.stdout
