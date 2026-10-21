from pathlib import Path

import pytest
from pydantic import ValidationError

from closinglineup.settings import Settings

DSN = "postgresql://closinglineup@localhost/closinglineup"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DSN)

    settings = Settings()

    assert settings.database_url == DSN
    assert settings.data_dir == Path("/data")
    assert str(settings.tz) == "America/New_York"
    assert settings.nba_rps == 1.0
    assert settings.nba_timeout_seconds == 30
    assert settings.healthchecks_url_nightly is None
    assert settings.healthchecks_url_daily is None
    assert settings.healthchecks_url_weekly is None
    assert settings.epm_enabled is False
    assert settings.darko_enabled is False
    assert settings.spotrac_enabled is False
    assert settings.garbage_time_rule == ((25, 540), (20, 360), (15, 180), (10, 60))
    assert settings.tier_starter_min == (4, 5)
    assert settings.tier_core_share == 0.18
    assert settings.tier_fringe_played_rate == 0.5
    assert settings.log_level == "INFO"
    assert settings.season is None


def test_missing_database_url_rejected() -> None:
    with pytest.raises(ValidationError, match="database_url"):
        Settings()


def test_overrides_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.setenv("DATA_DIR", "/tmp/cl")
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("NBA_RPS", "0.5")
    monkeypatch.setenv("HEALTHCHECKS_URL_NIGHTLY", "https://hc-ping.com/abc")
    monkeypatch.setenv("EPM_ENABLED", "true")
    monkeypatch.setenv("GARBAGE_TIME_RULE", " 30:600, 12:90 ")
    monkeypatch.setenv("TIER_STARTER_MIN", "3/4")
    monkeypatch.setenv("SEASON", "2025-26")

    settings = Settings()

    assert settings.data_dir == Path("/tmp/cl")
    assert str(settings.tz) == "UTC"
    assert settings.nba_rps == 0.5
    assert settings.healthchecks_url_nightly == "https://hc-ping.com/abc"
    assert settings.epm_enabled is True
    assert settings.garbage_time_rule == ((30, 600), (12, 90))
    assert settings.tier_starter_min == (3, 4)
    assert settings.season == "2025-26"


def test_empty_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.setenv("HEALTHCHECKS_URL_DAILY", "")
    monkeypatch.setenv("SEASON", "")

    settings = Settings()

    assert settings.healthchecks_url_daily is None
    assert settings.season is None


def test_log_level_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.setenv("LOG_LEVEL", "debug")

    assert Settings().log_level == "DEBUG"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GARBAGE_TIME_RULE", "25-540"),
        ("GARBAGE_TIME_RULE", "25:abc"),
        ("GARBAGE_TIME_RULE", " , "),
        ("TIER_STARTER_MIN", "4"),
        ("TIER_STARTER_MIN", "6/5"),
        ("TIER_STARTER_MIN", "0/5"),
        ("TIER_CORE_SHARE", "1.5"),
        ("TIER_FRINGE_PLAYED_RATE", "-0.1"),
        ("NBA_RPS", "0"),
        ("SEASON", "2025-2026"),
        ("TZ", "Not/AZone"),
        ("LOG_LEVEL", "LOUD"),
    ],
)
def test_malformed_values_rejected(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match=name.lower()):
        Settings()
