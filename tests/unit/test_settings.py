from pathlib import Path

import pytest

from job_hunter.settings import ConfigurationError, load_settings, load_user_config

ROOT = Path(__file__).resolve().parents[2]


def test_sample_configs(monkeypatch):
    monkeypatch.chdir(ROOT)
    preferences, candidate = load_user_config()
    assert preferences.salary.hard_min_acceptable is None
    if not Path("config/candidate.yaml").exists():
        assert candidate.languages.english_fluent is None


def test_environment_cannot_enable_external_actions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENABLE_EXTERNAL_ACTIONS", "true")
    with pytest.raises(ConfigurationError, match="ENABLE_EXTERNAL_ACTIONS"):
        load_settings()


def test_doctor_does_not_create_jd_database(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from job_hunter import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_user_config", lambda: (None, None))
    binary=tmp_path/"test-engine"
    binary.touch()
    monkeypatch.setattr(cli,"load_settings",lambda: type("Settings",(),{"agent_browser_binary":binary,"missing_model_settings":lambda self: []})())
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".local").exists()
