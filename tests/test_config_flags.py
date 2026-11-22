"""One parser for every boolean setting, and .env.example in step with app/config.py."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import config

ROOT = Path(__file__).resolve().parent.parent
CONFIG_SOURCE = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")

# Read by app/config.py but not ours to template: the platform sets the first two (Railway/
# Render), the third is the old key name that .env.example only mentions next to TEAM_API_KEY.
NOT_IN_EXAMPLE = {"RAILWAY_GIT_COMMIT_SHA", "PORT", "HACKLOOP_API_KEY"}


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "on", " On "])
def test_flag_on_spellings(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("SOME_TEST_FLAG", raw)
    assert config._flag("SOME_TEST_FLAG") is True
    assert config._flag("SOME_TEST_FLAG", True) is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "No", "off", " Off "])
def test_flag_off_spellings(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("SOME_TEST_FLAG", raw)
    assert config._flag("SOME_TEST_FLAG") is False
    assert config._flag("SOME_TEST_FLAG", True) is False


@pytest.mark.parametrize("raw", ["", "  ", "maybe", "2", "enabled"])
def test_flag_empty_or_unknown_keeps_default(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("SOME_TEST_FLAG", raw)
    assert config._flag("SOME_TEST_FLAG") is False
    assert config._flag("SOME_TEST_FLAG", True) is True


def test_flag_unset_keeps_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOME_TEST_FLAG", raising=False)
    assert config._flag("SOME_TEST_FLAG") is False
    assert config._flag("SOME_TEST_FLAG", True) is True


def test_no_boolean_is_parsed_by_hand() -> None:
    """A comparison against a truth word outside _flag is a second idea of truth."""
    body = CONFIG_SOURCE.split("def _domains", 1)[1]
    assert not re.search(r'"(?:true|false|yes|no|on)"', body)


def _config_env_names() -> set[str]:
    return set(re.findall(r'_(?:env|flag|domains)\(\s*"([A-Z0-9_]+)"', CONFIG_SOURCE))


def test_every_config_variable_is_in_env_example() -> None:
    """A commented `# NAME=` line counts: some defaults are paths or deprecated aliases."""
    listed = set(re.findall(r"^(?:# ?)?([A-Z0-9_]+)=", ENV_EXAMPLE, re.MULTILINE))
    missing = _config_env_names() - listed - NOT_IN_EXAMPLE
    assert not missing, f"add to .env.example (with the default): {sorted(missing)}"


def test_every_env_example_line_is_read_in_app() -> None:
    app_source = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((ROOT / "app").rglob("*.py"))
    )
    names = re.findall(r"^([A-Z0-9_]+)=", ENV_EXAMPLE, re.MULTILINE)
    unread = [n for n in names if f'"{n}"' not in app_source]
    assert not unread, f".env.example lines nothing in app/ reads: {unread}"
