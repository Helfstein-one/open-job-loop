from typer.testing import CliRunner

from src.presentation.cli import app

runner = CliRunner()


def test_cli_onboard_mock_non_interactive(tmp_path):
    db_file = str(tmp_path / "onboard_test.duckdb")
    yaml_file = str(tmp_path / "onboard_profile.yaml")

    result = runner.invoke(
        app,
        [
            "onboard",
            "--mock",
            "--non-interactive",
            "--db",
            db_file,
            "--output",
            yaml_file,
        ],
    )
    assert result.exit_code == 0
    assert "Perfil Alvo de Vagas Proposto" in result.stdout
    assert "Iniciando Entrevista Adaptativa (Grill-Me)" in result.stdout
    assert "Onboarding Concluído com Sucesso" in result.stdout
