"""Hand-labelled 2025-26 cases for the tier, event and flag rules, run on real data (spec §11).

`tests/derive/cases/*.yml` each hold a list of cases: a player on a team on a game date, the tier
and the flags he should have there, and a `note` naming the public evidence the label was read
from (NBA.com box scores, the official injury report). A case may also give `expected_badges`.

The data is `tests/fixtures/derive/`: a few teams' 2025-26 games through the last case's date,
exported from kumadrome with the official injury reports and NBA.com's player movement (its
README says how). It is loaded into a database of its own, beside the one `DATABASE_URL` names,
so its real season never meets the other tests' rows; `derive` runs over every game date it holds,
and each case is asserted against what it wrote.
"""

import gzip
import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
import yaml
from psycopg import sql
from sqlalchemy import create_engine, func, make_url, select, text
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.clock import FakeClock
from closinglineup.db.models import Game, PlayerFlag, RotationEvent, RotationState, RunStatus
from closinglineup.derive.run import DeriveRules, derive
from closinglineup.settings import Settings
from closinglineup.sources.cache import RawCache
from closinglineup.teams import by_tricode
from tests.test_migrations import alembic

CASES = Path(__file__).parent / "derive" / "cases"
FIXTURES = Path(__file__).parent / "fixtures" / "derive"
# Load order: a table after those it references.
TABLES = (
    "player",
    "game",
    "player_game",
    "stint",
    "roster_membership",
    "injury_status",
    "transaction",
)
CLOCK = datetime(2026, 10, 23, 12, tzinfo=UTC)

# Cases left out of the assertions, by `id`, each with the reason.
EXCLUDED: dict[str, str] = {}


def _cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(CASES.glob("*.yml")):
        cases.extend(yaml.safe_load(path.read_text()))
    return cases


def _load(database_url: str) -> None:
    url = make_url(database_url).set(drivername="postgresql")
    with psycopg.connect(url.render_as_string(hide_password=False)) as conn:
        for table in TABLES:
            with gzip.open(FIXTURES / f"{table}.csv.gz", "rt") as rows:
                columns = rows.readline().strip()
                statement = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv)").format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(map(sql.Identifier, columns.split(","))),
                )
                with conn.cursor().copy(statement) as copy:
                    for line in rows:
                        copy.write(line)


@pytest.fixture(scope="module")
def derived(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sessionmaker[Session]]:
    """The fixture loaded into a fresh database and derived over every game date it holds."""
    base = os.environ.get("DATABASE_URL")
    if not base:
        pytest.skip("DATABASE_URL is not set; run inside `docker compose exec app`")
    url = make_url(base)
    name = f"{url.database}_derive_cases"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    database_url = url.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(database_url)
    try:
        upgrade = alembic("upgrade", "head", database_url=database_url)
        assert upgrade.returncode == 0, upgrade.stderr
        _load(database_url)
        db = sessionmaker(engine, expire_on_commit=False)
        results = _derive(db, tmp_path_factory.mktemp("cache"))
        assert {r.status for r in results} == {RunStatus.SUCCEEDED}
        yield db
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _derive(db: sessionmaker[Session], cache: Path) -> list[Any]:
    with db() as session:
        days = sorted(set(session.scalars(select(Game.game_date))))
    unused = "postgresql+psycopg://unused@127.0.0.1:1/x"
    rules = DeriveRules.from_settings(Settings(database_url=unused, tz=ZoneInfo("UTC")))
    return derive(db, RawCache(cache), FakeClock(CLOCK), days, rules)


def _counts(db: sessionmaker[Session]) -> dict[str, int]:
    with db() as session:
        return {
            model.__tablename__: session.scalar(select(func.count()).select_from(model)) or 0
            for model in (RotationState, RotationEvent, PlayerFlag)
        }


def test_there_are_enough_cases_and_every_tier_and_flag_is_covered() -> None:
    cases = _cases()
    assert len(cases) >= 30
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["expected_tier"] for case in cases} >= {
        "injured",
        "starter",
        "core",
        "fringe",
        "out",
    }
    flags = {flag for case in cases for flag in case["expected_flags"]}
    assert flags >= {
        "minutes_cliff",
        "usage_up_efficiency_down",
        "bleeding",
        "earning_trust",
        "yo_yo",
        "contract_pressure",
    }
    assert all(case["note"].strip() for case in cases)


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["id"])
def test_case(derived: sessionmaker[Session], case: dict[str, Any]) -> None:
    if case["id"] in EXCLUDED:
        pytest.skip(EXCLUDED[case["id"]])
    team_id = by_tricode(case["team"]).nba_team_id
    day: date = case["game_date"]
    with derived() as session:
        state = session.get(RotationState, (case["player_id"], team_id, day))
        flags = set(
            session.scalars(
                select(PlayerFlag.flag).where(
                    PlayerFlag.player_id == case["player_id"],
                    PlayerFlag.team_id == team_id,
                    PlayerFlag.game_date == day,
                )
            )
        )
    assert state is not None, f"no rotation_state row for {case['player']} on {day}"
    assert (state.tier, flags) == (case["expected_tier"], set(case["expected_flags"]))
    if "expected_badges" in case:
        assert set(state.badges) == set(case["expected_badges"])


def test_a_second_derive_leaves_the_same_row_counts(
    derived: sessionmaker[Session], tmp_path: Path
) -> None:
    before = _counts(derived)

    results = _derive(derived, tmp_path)

    assert {r.status for r in results} == {RunStatus.SUCCEEDED}
    assert _counts(derived) == before
    assert all(before.values())
