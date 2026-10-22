import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.clock import FakeClock
from closinglineup.db.models import (
    Base,
    Game,
    LoadLog,
    Matchup,
    PlayerGame,
    Possession,
    RunStatus,
    Shot,
    Stint,
)
from closinglineup.load.loader import (
    NATURAL_KEYS,
    LoadError,
    LoadResult,
    load_dates,
    load_keyed,
    load_table,
    raise_for_failures,
)
from closinglineup.load.parquet import ROW_MODELS, ParquetStore
from closinglineup.parse.rows import PlayerGameRow, ShotRow, StintRow
from closinglineup.parse.shots import parse_shots
from closinglineup.pipeline import ParseReport, parse_date
from closinglineup.sources import nba_stats
from tests.test_nba_stats import fixture_body, season_fixture_body
from tests.test_pipeline import (
    DAY,
    DEN,
    GAME,
    GSW,
    PLAYER_GAMES,
    TZ,
    UNCACHED,
    cache_fixture_game,
)

START = datetime(2026, 10, 22, 12, tzinfo=UTC)
DAY_BEFORE = DAY - timedelta(days=1)
DATES = (DAY_BEFORE, DAY)

type Snapshot = dict[str, list[dict[str, Any]]]


def clear_fixture_game(db: sessionmaker[Session]) -> None:
    """Delete the fixture games' rows and the `load_log` rows of their dates, which earlier runs
    on the same database may have left. Players and roster memberships stay: they load by
    upsert, so a leftover row only reads as unchanged."""
    games = (GAME, UNCACHED)
    with db.begin() as session:
        for model in (Stint, Possession, Shot):
            session.execute(delete(model).where(model.game_id.in_(games)))
        session.execute(delete(Matchup).where(Matchup.game_id.in_(games)))
        session.execute(delete(PlayerGame).where(PlayerGame.game_id.in_(games)))
        session.execute(delete(Game).where(Game.game_id.in_(games)))
        session.execute(delete(LoadLog).where(LoadLog.game_date.in_(DATES)))


@pytest.fixture
def clean_db(db: sessionmaker[Session]) -> sessionmaker[Session]:
    clear_fixture_game(db)
    return db


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def store(tmp_path: Path) -> ParquetStore:
    return ParquetStore(tmp_path)


@pytest.fixture
def parsed(tmp_path: Path, store: ParquetStore) -> ParseReport:
    cache = cache_fixture_game(tmp_path)
    parse_date(cache, store, DAY_BEFORE, TZ)
    return parse_date(cache, store, DAY, TZ)


def _snapshot(db: sessionmaker[Session], store: ParquetStore, day: date) -> Snapshot:
    """Every column, timestamps included, of the rows the date's files hold, by table."""
    snapshot: Snapshot = {}
    with db() as session:
        for name in ROW_MODELS:
            target = Base.metadata.tables[name]
            key = NATURAL_KEYS[name]
            data = store.read(name, day)
            keys = list(zip(*(data.column(k).to_pylist() for k in key), strict=True))
            found = session.execute(
                select(target)
                .where(tuple_(*(target.c[k] for k in key)).in_(keys))
                .order_by(*(target.c[k] for k in key))
            )
            snapshot[name] = [dict(row._mapping) for row in found]  # pyright: ignore[reportPrivateUsage]
    return snapshot


def _logs(db: sessionmaker[Session], day: date) -> list[LoadLog]:
    with db() as session:
        query = select(LoadLog).where(LoadLog.game_date == day).order_by(LoadLog.table_name)
        return list(session.scalars(query))


def test_parse_then_load_fills_every_table_and_a_reload_changes_no_row(
    clean_db: sessionmaker[Session], store: ParquetStore, clock: FakeClock, parsed: ParseReport
) -> None:
    first = load_dates(clean_db, store, clock, [DAY])

    assert [(r.table, r.status, r.rows) for r in first] == [
        (name, RunStatus.SUCCEEDED, parsed.rows[name]) for name in ROW_MODELS
    ]
    snapshot = _snapshot(clean_db, store, DAY)
    assert [row["game_id"] for row in snapshot["game"]] == [GAME, UNCACHED]
    assert len(snapshot["player_game"]) == PLAYER_GAMES
    assert {name: len(rows) for name, rows in snapshot.items()} == parsed.rows

    clock.advance(timedelta(hours=1))
    second = load_dates(clean_db, store, clock, [DAY])

    assert [(r.status, r.changed) for r in second] == [(RunStatus.SUCCEEDED, 0)] * len(ROW_MODELS)
    assert _snapshot(clean_db, store, DAY) == snapshot
    # One `load_log` row per table, refreshed by the second load.
    logs = _logs(clean_db, DAY)
    assert sorted(log.table_name for log in logs) == sorted(ROW_MODELS)
    for log in logs:
        assert (log.step, log.status, log.started_at) == ("load", "succeeded", clock.now())
        assert log.detail == {"rows": parsed.rows[log.table_name], "changed": 0, "removed": 0}


def test_a_changed_row_is_updated_in_place(
    clean_db: sessionmaker[Session], store: ParquetStore, clock: FakeClock, parsed: ParseReport
) -> None:
    load_dates(clean_db, store, clock, [DAY])
    rows = [PlayerGameRow.model_validate(r) for r in store.read("player_game", DAY).to_pylist()]
    edited = rows[0].model_copy(update={"pts": rows[0].pts + 1})
    store.write_rows("player_game", DAY, [edited, *rows[1:]])

    [result] = [r for r in load_dates(clean_db, store, clock, [DAY]) if r.table == "player_game"]

    assert (result.rows, result.changed) == (PLAYER_GAMES, 1)
    with clean_db() as session:
        found = session.get_one(PlayerGame, (GAME, edited.player_id))
        assert found.pts == edited.pts


def test_a_load_that_fails_rolls_back_and_records_why(
    clean_db: sessionmaker[Session], store: ParquetStore, clock: FakeClock, parsed: ParseReport
) -> None:
    # Box score rows of a game with no `game` row break `player_game`'s foreign key.
    result = load_table(clean_db, store, clock, "player_game", DAY)

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.startswith("IntegrityError")
    with clean_db() as session:
        count = select(func.count()).select_from(PlayerGame).where(PlayerGame.game_id == GAME)
        assert session.scalar(count) == 0
    [log] = _logs(clean_db, DAY)
    assert (log.table_name, log.status) == ("player_game", "failed")
    assert log.detail == {"error": result.error}
    with pytest.raises(LoadError, match=r"load failed: player_game 2025-10-23: IntegrityError"):
        raise_for_failures([result])


def test_a_date_never_parsed_fails_to_load(
    clean_db: sessionmaker[Session], store: ParquetStore, clock: FakeClock
) -> None:
    [game, *_] = load_dates(clean_db, store, clock, [DAY])

    assert game.status is RunStatus.FAILED
    assert game.error is not None
    assert game.error.startswith("FileNotFoundError")


def test_missing_skips_a_table_the_date_has_no_file_for(
    clean_db: sessionmaker[Session], store: ParquetStore, clock: FakeClock
) -> None:
    # A date parsed before `stint` existed: every other table's file, none for `stint`.
    for name in ROW_MODELS:
        if name != "stint":
            store.write_rows(name, DAY_BEFORE, [])

    results = load_dates(clean_db, store, clock, missing=True)

    assert [r.table for r in results] == [name for name in ROW_MODELS if name != "stint"]
    assert {r.status for r in results} == {RunStatus.SUCCEEDED}


def test_missing_reloads_only_what_has_no_successful_load(
    clean_db: sessionmaker[Session], store: ParquetStore, clock: FakeClock, parsed: ParseReport
) -> None:
    load_dates(clean_db, store, clock, DATES)
    with clean_db.begin() as session:
        session.execute(
            delete(LoadLog).where(LoadLog.table_name == "game", LoadLog.game_date == DAY_BEFORE)
        )
    clock.advance(timedelta(hours=1))

    results = load_dates(clean_db, store, clock, missing=True)

    assert results == [LoadResult("game", DAY_BEFORE, RunStatus.SUCCEEDED, 0, 0)]
    assert {log.table_name: log.started_at for log in _logs(clean_db, DAY_BEFORE)}["game"] == (
        clock.now()
    )
    assert {log.started_at for log in _logs(clean_db, DAY)} == {START}


def test_stints_add_up_to_the_final_score_and_the_box_scores_possessions(
    clean_db: sessionmaker[Session], store: ParquetStore, clock: FakeClock, parsed: ParseReport
) -> None:
    raise_for_failures(load_dates(clean_db, store, clock, [DAY]))
    advanced = json.loads(fixture_body(nba_stats.BOXSCORE_ADVANCED_V3))["boxScoreAdvanced"]
    box_possessions = {
        advanced[side]["teamId"]: advanced[side]["statistics"]["possessions"]
        for side in ("homeTeam", "awayTeam")
    }

    with clean_db() as session:
        totals = {
            team: (pts, possessions)
            for team, pts, possessions in session.execute(
                select(Stint.team_id, func.sum(Stint.pts_for), func.sum(Stint.possessions))
                .where(Stint.game_id == GAME)
                .group_by(Stint.team_id)
            )
        }

        counted = dict(
            session.execute(
                select(Possession.offense_team_id, func.count())
                .where(Possession.game_id == GAME, Possession.counted)
                .group_by(Possession.offense_team_id)
            ).all()
        )

    assert {team: pts for team, (pts, _) in totals.items()} == {GSW: 137, DEN: 131}
    for team, (_, possessions) in totals.items():
        assert abs(possessions - box_possessions[team]) < 3, team
        assert counted[team] == possessions, team


def _stints(db: sessionmaker[Session]) -> list[tuple[int, int, float]]:
    with db() as session:
        found = session.execute(
            select(Stint.team_id, Stint.period, Stint.start_sec).where(Stint.game_id == GAME)
        )
        return sorted((team, period, start) for team, period, start in found)


def test_a_reparsed_games_stints_replace_the_ones_loaded_before(
    clean_db: sessionmaker[Session], store: ParquetStore, clock: FakeClock, parsed: ParseReport
) -> None:
    raise_for_failures(load_dates(clean_db, store, clock, [DAY]))
    rows = [StintRow.model_validate(r) for r in store.read("stint", DAY).to_pylist()]
    # An override that moves one stint's start and drops another, as a re-parse can.
    moved = rows[0].model_copy(update={"start_sec": rows[0].start_sec + 0.5})
    store.write_rows("stint", DAY, [moved, *rows[2:]])

    [result] = [r for r in load_dates(clean_db, store, clock, [DAY]) if r.table == "stint"]

    assert (result.rows, result.changed, result.removed) == (len(rows) - 1, 1, 2)
    expected = sorted((r.team_id, r.period, r.start_sec) for r in [moved, *rows[2:]])
    assert _stints(clean_db) == expected


CURRY_SHOTS = "201939_2025-26_regular-season"
OTHER_SHOTS = "1_2025-26_regular-season"


def _shot_keys(db: sessionmaker[Session]) -> list[tuple[int, int]]:
    with db() as session:
        found = session.execute(select(Shot.player_id, Shot.event_num).where(Shot.game_id == GAME))
        return sorted((player, event) for player, event in found)


def test_reloading_a_players_shots_replaces_them_rather_than_adding_to_them(
    clean_db: sessionmaker[Session], store: ParquetStore, clock: FakeClock, parsed: ParseReport
) -> None:
    raise_for_failures(load_dates(clean_db, store, clock, [DAY]))
    shots = parse_shots(json.loads(season_fixture_body(nba_stats.SHOT_CHART_DETAIL)))
    other = shots[0].model_copy(update={"player_id": 1, "event_num": 9999})
    store.write_rows("shot", CURRY_SHOTS, shots)
    store.write_rows("shot", OTHER_SHOTS, [other])

    first = load_keyed(clean_db, store, clock, "shot")
    second = load_keyed(clean_db, store, clock, "shot")

    assert [(r.key, r.rows, r.changed) for r in first] == [
        (OTHER_SHOTS, 1, 1),
        (CURRY_SHOTS, 25, 25),
    ]
    assert [(r.rows, r.changed, r.removed) for r in second] == [(1, 0, 0), (25, 0, 0)]
    assert len(_shot_keys(clean_db)) == 26

    # A re-fetch that credits one of his shots elsewhere and corrects another.
    corrected = shots[1].model_copy(update={"made": not shots[1].made})
    store.write_rows("shot", CURRY_SHOTS, [corrected, *shots[2:]])

    [result] = load_keyed(clean_db, store, clock, "shot", [CURRY_SHOTS])

    assert (result.rows, result.changed, result.removed) == (24, 1, 1)
    expected = [(1, 9999), *sorted((s.player_id, s.event_num) for s in shots[1:])]
    assert _shot_keys(clean_db) == expected
    with clean_db() as session:
        found = session.scalars(
            select(Shot).where(Shot.game_id == GAME, Shot.event_num == corrected.event_num)
        ).one()
        assert ShotRow.model_validate(found, from_attributes=True) == corrected
    # Shot files have no game date, so no `load_log` row.
    assert {log.table_name for log in _logs(clean_db, DAY)} == set(ROW_MODELS)
