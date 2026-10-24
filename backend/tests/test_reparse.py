import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from closinglineup.cli import app
from closinglineup.db.models import (
    Base,
    ImpactSnapshot,
    PlayerFlag,
    RotationEvent,
    RotationState,
    Stint,
)
from closinglineup.reparse import uncached_games
from closinglineup.sources import nba_cdn
from closinglineup.sources.nba_http import NbaSession
from tests.test_loader import clear_fixture_game
from tests.test_pipeline import DAY, GAME, SCHEDULE, UNCACHED, cache_fixture_game

runner = CliRunner()

# Every stats.nba.com per-game endpoint `nightly` caches, as `reparse` lists them for a game with
# none cached.
STATS_ENDPOINTS = [
    "boxscoretraditionalv3",
    "boxscoretraditionalv2",
    "boxscoreadvancedv3",
    "boxscorehustlev2",
    "boxscorematchupsv3",
    "boxscoreusagev3",
    "playbyplayv3",
]
NO_STATS = ", ".join(f"nba_stats/{endpoint}" for endpoint in STATS_ENDPOINTS)

type Snapshot = dict[str, list[dict[str, Any]]]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every request an `NbaSession` is asked to make, each refused."""
    made: list[str] = []

    def get(self: NbaSession, url: str, params: Any = None) -> Any:
        made.append(url)
        raise AssertionError(f"reparse asked NBA.com for {url}")

    monkeypatch.setattr(NbaSession, "get", get)
    return made


def _env(monkeypatch: pytest.MonkeyPatch, database_url: str, data_dir: Path) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DATA_DIR", str(data_dir))


def _cache_only_the_fixture_game(data_dir: Path) -> None:
    """The fixture game, fully cached, alone on its date: the other final game is uncached."""
    cache = cache_fixture_game(data_dir)
    schedule = json.loads(json.dumps(SCHEDULE))
    [game_date] = schedule["leagueSchedule"]["gameDates"]
    game_date["games"] = [g for g in game_date["games"] if g["gameId"] == GAME]
    body = json.dumps(schedule).encode()
    cache.put(nba_cdn.SOURCE, nba_cdn.SCHEDULE, "2025-26", body, url="u", status=200, force=True)


def _rows(session: Session, model: type[Base], *where: Any) -> list[dict[str, Any]]:
    """The rows of `model` matching `where`, without their timestamps or surrogate IDs."""
    skip = {"created_at", "updated_at", "id"}
    columns = [c for c in model.__table__.columns if c.name not in skip]
    found = session.execute(select(*columns).where(*where)).mappings()
    return sorted((dict(row) for row in found), key=repr)


def _derived(db: sessionmaker[Session]) -> Snapshot:
    """What `derive` wrote for the fixture game and its date."""
    with db() as session:
        windows = session.execute(
            text("SELECT * FROM player_window_stats WHERE game_date = :day"), {"day": DAY}
        )
        return {
            "stint": _rows(session, Stint, Stint.game_id == GAME),
            "rotation_state": _rows(session, RotationState, RotationState.game_date == DAY),
            "rotation_event": _rows(session, RotationEvent, RotationEvent.game_date == DAY),
            "player_flag": _rows(session, PlayerFlag, PlayerFlag.game_date == DAY),
            "impact_snapshot": _rows(session, ImpactSnapshot, ImpactSnapshot.date == DAY),
            "player_window_stats": sorted((dict(r) for r in windows.mappings()), key=repr),
        }


def _forget(db: sessionmaker[Session]) -> None:
    """Delete everything loaded and derived for the fixture game and its date."""
    with db.begin() as session:
        for model in (RotationState, RotationEvent, PlayerFlag):
            session.execute(delete(model).where(model.game_date == DAY))
        session.execute(delete(ImpactSnapshot).where(ImpactSnapshot.date == DAY))
    clear_fixture_game(db)


def test_reparse_reproduces_the_derived_rows_from_the_raw_cache_alone(
    db: sessionmaker[Session],
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
) -> None:
    _forget(db)
    _cache_only_the_fixture_game(tmp_path)
    _env(monkeypatch, database_url, tmp_path)
    day = DAY.isoformat()
    for args in (
        ["parse", "--date", day],
        ["load", "--date", day],
        ["load", "--memberships"],
        ["derive", "--date", day],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
    derived = _derived(db)
    assert derived["stint"] and derived["rotation_state"] and derived["impact_snapshot"]
    _forget(db)
    shutil.rmtree(tmp_path / "parquet")
    assert _derived(db)["rotation_state"] == []

    result = runner.invoke(app, ["reparse", "--from", day, "--to", day])

    assert result.exit_code == 0, result.output
    assert f"reparse {day} to {day}: 1 dates, 1 games parsed" in result.output
    assert _derived(db) == derived
    assert calls == []


def test_reparse_refuses_listing_each_game_that_lacks_its_cached_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calls: list[str]
) -> None:
    # The fixture game without its play-by-play, beside a final game with nothing cached.
    cache_fixture_game(tmp_path, pbp=False)
    _env(monkeypatch, "postgresql+psycopg://unused@127.0.0.1:1/x", tmp_path)

    result = runner.invoke(app, ["reparse", "--from", DAY.isoformat(), "--to", DAY.isoformat()])

    assert result.exit_code == 1
    assert result.output.splitlines() == [
        f"reparse {DAY} {GAME}: not in the raw cache: cdn/playbyplay",
        f"reparse {DAY} {UNCACHED}: not in the raw cache: {NO_STATS}, or cdn/boxscore in their "
        "place; cdn/playbyplay",
        "reparse: refused: 2 final games lack cached responses; fetch them first",
    ]
    assert not (tmp_path / "parquet").exists()
    assert calls == []


def test_reparse_refuses_a_date_whose_schedule_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, "postgresql+psycopg://unused@127.0.0.1:1/x", tmp_path)

    result = runner.invoke(app, ["reparse", "--from", DAY.isoformat(), "--to", DAY.isoformat()])

    assert result.exit_code == 1
    assert "reparse: refused: no 2025-26 schedule in the raw cache" in result.output


def test_a_game_parsed_from_the_live_box_needs_no_stats_nba_com_responses(tmp_path: Path) -> None:
    cache = cache_fixture_game(tmp_path, stats=False, live_box=True)

    assert uncached_games(cache, [DAY]) == {
        DAY: {UNCACHED: [f"{NO_STATS}, or cdn/boxscore in their place", "cdn/playbyplay"]}
    }


def test_reparse_takes_only_known_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, "postgresql+psycopg://unused@127.0.0.1:1/x", tmp_path)

    result = runner.invoke(
        app, ["reparse", "--from", "2025-10-23", "--to", "2025-10-23", "--tables", "stint,stints"]
    )

    assert result.exit_code == 2
    assert "unknown tables stints" in result.output
