from pathlib import Path

from pydantic import BaseModel
from typer.testing import CliRunner

from monitor.cli import app
from monitor.settings import Settings

ROOT = Path(__file__).resolve().parent.parent


def setting_names() -> set[str]:
    names = set()
    for group, field in Settings.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            for child in annotation.model_fields:
                names.add(f"{group}__{child}".upper())
        else:
            names.add(group.upper())
    return names


def test_env_example_lists_every_setting():
    lines = (ROOT / ".env.example").read_text().splitlines()
    listed = {
        line.split("=", 1)[0]
        for line in lines
        if line and not line.startswith("#")
    }
    assert listed == setting_names()


def test_nested_override_from_environment(monkeypatch):
    monkeypatch.setenv("LLM__DAILY_USD_CAP", "12.5")
    monkeypatch.setenv("POLL__TIER_SECONDS", "[10,20,30]")
    settings = Settings()
    assert settings.llm.daily_usd_cap == 12.5
    assert settings.poll.tier_seconds == [10, 20, 30]


def test_cli_lists_every_command():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "init",
        "verify",
        "sources",
        "ref",
        "pipeline",
        "web",
        "rollups",
        "brief",
        "disk",
        "prune",
        "backup",
        "replay-check",
        "cost",
    ):
        assert command in result.output


def test_user_agents_carry_no_email_address():
    from monitor.ref.wiki import USER_AGENT

    for agent in (Settings().poll.user_agent, USER_AGENT):
        assert "@" not in agent
