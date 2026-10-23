"""ORM models. Alembic autogenerates migrations against `Base.metadata` (spec §6)."""

# `dt.date` for a column named `date`, whose attribute would otherwise shadow the type.
import datetime as dt
from datetime import date, datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names, so migrations can drop and alter what they created.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class RunStatus(StrEnum):
    """A `job_run` or `load_log` row's status."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


_STATUS_CHECK = "status IN ('running', 'succeeded', 'failed')"

# Categorical columns are text with a CHECK rather than Postgres enums, so a new value is a
# constraint change rather than an enum type migration (NEU-1860).
CONFERENCES = ("East", "West")
DIVISIONS = ("Atlantic", "Central", "Southeast", "Northwest", "Pacific", "Southwest")
CONTRACT_TYPES = ("standard", "two_way", "ten_day", "exhibit_10")
GAME_PHASES = ("regular", "cup_knockout", "play_in", "playoff")
GAME_STATUSES = ("scheduled", "final", "postponed")
DNP_REASONS = ("cd", "injury", "inactive", "suspension", "g_league", "other")
BOX_SOURCES = ("stats", "cdn")
SHOT_ZONES = ("rim", "short_mid", "long_mid", "corner_3", "above_break_3")
# Spec §4.3: the official report's own spelling; ESPN's statuses are mapped onto these.
INJURY_STATUSES = ("Out", "Doubtful", "Questionable", "Probable", "Available")
INJURY_SOURCES = ("pdf", "espn")
TRANSACTION_TYPES = (
    "sign",
    "waive",
    "trade",
    "two_way_sign",
    "two_way_convert",
    "assign",
    "recall",
    "option_exercise",
    "option_decline",
    "other",
)
IMPACT_SOURCES = ("epm", "darko", "bpm_local")
# Not enumerated in spec §4.6: a player, team or early termination option.
OPTION_TYPES = ("player", "team", "early_termination")
# Spec §4.6: Spotrac, with RealGM the fallback behind the same row type.
CONTRACT_SOURCES = ("spotrac", "realgm")


def _one_of(column: str, values: tuple[str, ...]) -> CheckConstraint:
    """A CHECK that `column` holds one of `values`; NULL passes, as with any CHECK."""
    listed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({listed})", name=column)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        datetime: DateTime(timezone=True),
    }


class Timestamps:
    """`created_at` / `updated_at`, which every table carries (spec §6)."""

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class LoadLog(Timestamps, Base):
    """What ran for one (table, game date, step); `fetch --missing` and `load --missing` read it
    to self-heal skipped dates (spec §3.2)."""

    __tablename__ = "load_log"
    __table_args__ = (CheckConstraint(_STATUS_CHECK, name="status"),)

    table_name: Mapped[str] = mapped_column(String, primary_key=True)
    game_date: Mapped[date] = mapped_column(primary_key=True)
    step: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    detail: Mapped[dict[str, Any] | None]


class JobRun(Timestamps, Base):
    """One run of a scheduled job (spec §3.3)."""

    __tablename__ = "job_run"
    __table_args__ = (CheckConstraint(_STATUS_CHECK, name="status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String, index=True)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String)
    detail: Mapped[dict[str, Any] | None]


# Reference (spec §6.1). Per-game rows reference `team` and `game`, which the migration seeds and
# the schedule fills ahead of any box score. They do not reference `player`, which the daily
# roster pull fills: a player signed today can play tonight, before his `player` row exists.


class Team(Timestamps, Base):
    """The 30 franchises, seeded from `closinglineup.teams` by the migration that creates it."""

    __tablename__ = "team"
    __table_args__ = (_one_of("conference", CONFERENCES), _one_of("division", DIVISIONS))

    nba_team_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    tricode: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    conference: Mapped[str] = mapped_column(String)
    division: Mapped[str] = mapped_column(String)


class Player(Timestamps, Base):
    __tablename__ = "player"

    nba_player_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String)
    birthdate: Mapped[date | None]
    draft_year: Mapped[int | None]
    draft_pick: Mapped[int | None]
    height_in: Mapped[int | None]
    position: Mapped[str | None] = mapped_column(String)


class RosterMembership(Timestamps, Base):
    """A player's span on a team, derived nightly from rosters and transactions; open-ended while
    `end_date` is NULL. A player is on one team per date: `derive` keeps spans from
    overlapping, since the table does not."""

    __tablename__ = "roster_membership"
    __table_args__ = (
        UniqueConstraint("player_id", "team_id", "start_date"),
        _one_of("contract_type", CONTRACT_TYPES),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    player_id: Mapped[int]
    team_id: Mapped[int] = mapped_column(ForeignKey("team.nba_team_id"))
    start_date: Mapped[date]
    end_date: Mapped[date | None]
    contract_type: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)


class Game(Timestamps, Base):
    """A counted game (spec §2): never preseason, All-Star or Summer League."""

    __tablename__ = "game"
    __table_args__ = (_one_of("phase", GAME_PHASES), _one_of("status", GAME_STATUSES))

    game_id: Mapped[str] = mapped_column(String, primary_key=True)
    season: Mapped[str] = mapped_column(String)
    game_date: Mapped[date] = mapped_column(index=True)
    phase: Mapped[str] = mapped_column(String)
    home_team_id: Mapped[int] = mapped_column(ForeignKey("team.nba_team_id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("team.nba_team_id"))
    status: Mapped[str] = mapped_column(String)
    home_score: Mapped[int | None]
    away_score: Mapped[int | None]
    home_b2b: Mapped[bool]
    away_b2b: Mapped[bool]


# Per game (spec §6.2).


class PlayerGame(Timestamps, Base):
    """One player's game for one team, DNPs included. Counting stats come from either source;
    the advanced and hustle columns are NULL where the source has none (cdn) or a DNP leaves
    them undefined."""

    __tablename__ = "player_game"
    __table_args__ = (
        Index(None, "player_id", "team_id"),
        _one_of("dnp_reason", DNP_REASONS),
        _one_of("source", BOX_SOURCES),
    )

    game_id: Mapped[str] = mapped_column(ForeignKey("game.game_id"), primary_key=True)
    player_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.nba_team_id"))
    is_starter: Mapped[bool]
    seconds: Mapped[int]
    dnp_reason: Mapped[str | None] = mapped_column(String)
    pts: Mapped[int]
    fgm: Mapped[int]
    fga: Mapped[int]
    fg3m: Mapped[int]
    fg3a: Mapped[int]
    ftm: Mapped[int]
    fta: Mapped[int]
    oreb: Mapped[int]
    dreb: Mapped[int]
    ast: Mapped[int]
    stl: Mapped[int]
    blk: Mapped[int]
    tov: Mapped[int]
    pf: Mapped[int]
    plus_minus: Mapped[int]
    ts_pct: Mapped[float | None]
    efg_pct: Mapped[float | None]
    usg_pct: Mapped[float | None]
    ast_pct: Mapped[float | None]
    tov_pct: Mapped[float | None]
    oreb_pct: Mapped[float | None]
    dreb_pct: Mapped[float | None]
    off_rating: Mapped[float | None]
    def_rating: Mapped[float | None]
    pace: Mapped[float | None]
    deflections: Mapped[int | None]
    contested_shots: Mapped[int | None]
    charges_drawn: Mapped[int | None]
    source: Mapped[str] = mapped_column(String)


class Stint(Timestamps, Base):
    """One team's view of a stint, as `parse.pbp.StintRow` emits it. `is_garbage_time` is NULL
    until `derive` classifies the stint (spec §7.4).

    The unique key leads with `(game_id, team_id)`, so it is also the index the views read by.
    """

    __tablename__ = "stint"
    __table_args__ = (UniqueConstraint("game_id", "team_id", "period", "start_sec"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("game.game_id"))
    period: Mapped[int]
    start_sec: Mapped[float]
    end_sec: Mapped[float]
    team_id: Mapped[int] = mapped_column(ForeignKey("team.nba_team_id"))
    lineup_key: Mapped[str] = mapped_column(String)
    opp_lineup_key: Mapped[str] = mapped_column(String)
    possessions: Mapped[int]
    pts_for: Mapped[int]
    pts_against: Mapped[int]
    is_garbage_time: Mapped[bool | None]


class Possession(Timestamps, Base):
    """One pbpstats possession, as `parse.pbp.PossessionRow` emits it. pbpstats numbers
    possessions from 1 within each period."""

    __tablename__ = "possession"
    __table_args__ = (UniqueConstraint("game_id", "period", "number"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("game.game_id"))
    period: Mapped[int]
    number: Mapped[int]
    offense_team_id: Mapped[int] = mapped_column(ForeignKey("team.nba_team_id"))
    start_sec: Mapped[float]
    end_sec: Mapped[float]
    start_score_margin: Mapped[int]
    start_type: Mapped[str | None] = mapped_column(String)
    has_timeout: Mapped[bool]
    previous_has_timeout: Mapped[bool]
    end_type: Mapped[str] = mapped_column(String)
    counted: Mapped[bool]


class PossessionLineup(Timestamps, Base):
    """One five NBA.com credits with a possession, as `parse.pbp.PossessionLineupRow` emits it;
    read only by reconciliation (NEU-1900)."""

    __tablename__ = "possession_lineup"
    __table_args__ = (UniqueConstraint("game_id", "period", "number", "team_id", "lineup_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("game.game_id"))
    period: Mapped[int]
    number: Mapped[int]
    team_id: Mapped[int] = mapped_column(ForeignKey("team.nba_team_id"))
    lineup_key: Mapped[str] = mapped_column(String)


class Shot(Timestamps, Base):
    """One field-goal attempt from `shotchartdetail`. `event_num` is its `GAME_EVENT_ID`, not in
    spec §6.2: it is the shot's natural key, which a reload upserts on."""

    __tablename__ = "shot"
    __table_args__ = (
        UniqueConstraint("game_id", "event_num"),
        Index(None, "player_id", "game_id"),
        _one_of("zone", SHOT_ZONES),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("game.game_id"))
    event_num: Mapped[int]
    player_id: Mapped[int]
    team_id: Mapped[int] = mapped_column(ForeignKey("team.nba_team_id"))
    period: Mapped[int]
    clock_sec: Mapped[int]
    x: Mapped[int]
    y: Mapped[int]
    distance_ft: Mapped[int]
    zone: Mapped[str] = mapped_column(String)
    shot_type: Mapped[str] = mapped_column(String)
    made: Mapped[bool]
    assisted_by: Mapped[int | None]
    shot_clock_bucket: Mapped[str | None] = mapped_column(String)


class Matchup(Timestamps, Base):
    """One offensive player against one defender in a game, from `boxscorematchupsv3`."""

    __tablename__ = "matchup"

    game_id: Mapped[str] = mapped_column(ForeignKey("game.game_id"), primary_key=True)
    off_player_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    def_player_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    partial_poss: Mapped[float]
    pts: Mapped[int]
    fgm: Mapped[int]
    fga: Mapped[int]
    fg3m: Mapped[int]
    fg3a: Mapped[int]
    tov: Mapped[int]
    ast: Mapped[int]


# Snapshots (spec §6.3): what the overlays write, each a dated view of a source rather than a
# game's record.


class InjuryStatus(Timestamps, Base):
    """One player's line in one injury report, the official PDF's or ESPN's (spec §4.3). `game_id`
    is NULL until the report's matchup is matched to a `game` row."""

    __tablename__ = "injury_status"
    __table_args__ = (
        UniqueConstraint("player_id", "game_date", "report_datetime", "source"),
        Index(None, "team_id", "report_datetime"),
        _one_of("status", INJURY_STATUSES),
        _one_of("source", INJURY_SOURCES),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_datetime: Mapped[datetime]
    game_date: Mapped[date]
    game_id: Mapped[str | None] = mapped_column(ForeignKey("game.game_id"))
    player_id: Mapped[int]
    team_id: Mapped[int] = mapped_column(ForeignKey("team.nba_team_id"))
    status: Mapped[str] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)


class Transaction(Timestamps, Base):
    """One NBA.com player-movement row, its `type` classified from the description (spec §4.4).
    A team outside the NBA, a G League affiliate for one, is NULL; `detail` names it."""

    __tablename__ = "transaction"
    __table_args__ = (
        UniqueConstraint("player_id", "date", "type", "detail"),
        _one_of("type", TRANSACTION_TYPES),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    date: Mapped[dt.date]
    type: Mapped[str] = mapped_column(String)
    player_id: Mapped[int]
    from_team_id: Mapped[int | None] = mapped_column(ForeignKey("team.nba_team_id"))
    to_team_id: Mapped[int | None] = mapped_column(ForeignKey("team.nba_team_id"))
    detail: Mapped[str] = mapped_column(String)


class ImpactSnapshot(Timestamps, Base):
    """One impact metric's value for a player on a date (spec §4.5). The O/D split and the
    minutes it rests on are NULL where the source publishes none."""

    __tablename__ = "impact_snapshot"
    __table_args__ = (_one_of("source", IMPACT_SOURCES),)

    date: Mapped[dt.date] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    source: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[float]
    o_value: Mapped[float | None]
    d_value: Mapped[float | None]
    minutes_basis: Mapped[float | None]


class Contract(Timestamps, Base):
    """A player's contract for one season as a team's cap page showed it on `as_of_date` (spec
    §4.6). `salary` and `guaranteed` are dollars: a partial guarantee is common.

    `team_id`, the team whose page lists it, is in its natural key: a waived player is on his old
    team's page as dead money and on his new team's with a live contract (NEU-1866)."""

    __tablename__ = "contract"
    __table_args__ = (
        UniqueConstraint("player_id", "team_id", "season", "as_of_date", "source"),
        _one_of("option_type", OPTION_TYPES),
        _one_of("source", CONTRACT_SOURCES),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    player_id: Mapped[int]
    team_id: Mapped[int] = mapped_column(ForeignKey("team.nba_team_id"))
    season: Mapped[str] = mapped_column(String)
    salary: Mapped[int]
    guaranteed: Mapped[int]
    option_type: Mapped[str | None] = mapped_column(String)
    option_date: Mapped[date | None]
    trade_eligible_date: Mapped[date | None]
    two_way: Mapped[bool]
    as_of_date: Mapped[date]
    source: Mapped[str] = mapped_column(String)
