"""Typed rows the parsers emit, one model per table (spec §6.1, §6.2).

Each model's fields are its table's columns less the ones the database fills (`id`,
`created_at`, `updated_at`). Validation is pydantic's lax mode, so a number NBA.com sends as a
string (`"202710"`, `"0.5"`) or a whole float (`8.0`) is coerced; a column the table allows to be
NULL defaults to None, so a source that leaves it out still parses. A game ID must be NBA.com's
ten digits: an integer-typed one has lost its leading zeros and is rejected rather than coerced.
"""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from closinglineup.season import Phase

type GameId = Annotated[str, StringConstraints(pattern=r"^\d{10}$")]
type ContractType = Literal["standard", "two_way", "ten_day", "exhibit_10"]
type GameStatus = Literal["scheduled", "final", "postponed"]
type DnpReason = Literal["cd", "injury", "inactive", "suspension", "g_league", "other"]
type BoxSource = Literal["stats", "cdn"]


class ParseError(ValueError):
    """A raw payload is not the shape its parser reads."""


class Row(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PlayerRow(Row):
    nba_player_id: int
    name: str
    birthdate: date | None = None
    draft_year: int | None = None
    draft_pick: int | None = None
    height_in: int | None = None
    position: str | None = None


class RosterMembershipRow(Row):
    player_id: int
    team_id: int
    start_date: date
    end_date: date | None = None
    contract_type: ContractType = "standard"
    source: str


class GameRow(Row):
    game_id: GameId
    season: str
    game_date: date
    phase: Phase
    home_team_id: int
    away_team_id: int
    status: GameStatus
    home_score: int | None = None
    away_score: int | None = None
    home_b2b: bool
    away_b2b: bool


class PlayerGameRow(Row):
    game_id: GameId
    player_id: int
    team_id: int
    is_starter: bool
    seconds: int
    dnp_reason: DnpReason | None = None
    pts: int
    fgm: int
    fga: int
    fg3m: int
    fg3a: int
    ftm: int
    fta: int
    oreb: int
    dreb: int
    ast: int
    stl: int
    blk: int
    tov: int
    pf: int
    plus_minus: int
    ts_pct: float | None = None
    efg_pct: float | None = None
    usg_pct: float | None = None
    ast_pct: float | None = None
    tov_pct: float | None = None
    oreb_pct: float | None = None
    dreb_pct: float | None = None
    off_rating: float | None = None
    def_rating: float | None = None
    pace: float | None = None
    deflections: int | None = None
    contested_shots: int | None = None
    charges_drawn: int | None = None
    source: BoxSource


class MatchupRow(Row):
    game_id: GameId
    off_player_id: int
    def_player_id: int
    partial_poss: float
    pts: int
    fgm: int
    fga: int
    fg3m: int
    fg3a: int
    tov: int
    ast: int
