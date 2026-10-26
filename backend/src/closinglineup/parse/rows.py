"""Typed rows the parsers emit, one model per table (spec §6.1, §6.2).

Each model's fields are its table's columns less the ones the database fills (`id`,
`created_at`, `updated_at`). Validation is pydantic's lax mode, so a number NBA.com sends as a
string (`"202710"`, `"0.5"`) or a whole float (`8.0`) is coerced; a column the table allows to be
NULL defaults to None, so a source that leaves it out still parses. A game ID must be NBA.com's
ten digits: an integer-typed one has lost its leading zeros and is rejected rather than coerced.
"""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from closinglineup.season import Phase

type GameId = Annotated[str, StringConstraints(pattern=r"^\d{10}$")]
type ContractType = Literal["standard", "two_way", "ten_day", "exhibit_10"]
type GameStatus = Literal["scheduled", "final", "postponed"]
type DnpReason = Literal["cd", "injury", "inactive", "suspension", "g_league", "other"]
type BoxSource = Literal["stats", "cdn"]
type ShotZone = Literal["rim", "short_mid", "long_mid", "corner_3", "above_break_3"]


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
    tip_time: datetime | None = None
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


class StintRow(Row):
    """One team's view of a stint (`stint`, less `is_garbage_time`, which `derive` fills).

    Times are seconds elapsed in the period. A lineup key is five player IDs, sorted as numbers,
    joined by `-`. `possessions` are the team's offensive possessions as pbpstats counts them
    (`count_as_possession`).
    """

    game_id: GameId
    period: int
    start_sec: float
    end_sec: float
    team_id: int
    lineup_key: str
    opp_lineup_key: str
    possessions: int
    pts_for: int
    pts_against: int


class PossessionRow(Row):
    """One pbpstats possession plus its game (`possession`).

    Times are seconds elapsed in the period. `counted` is whether pbpstats counts it as a real
    possession: one that starts with two seconds or less left and scores nothing is not.
    `start_type` is None where pbpstats cannot tell (after a rebound of a heave). `end_type` is
    the pbpstats event type that ended it: `FieldGoal`, `FreeThrow`, `Rebound` (a defensive one),
    `Turnover`, `EndOfPeriod` and so on.
    """

    game_id: GameId
    period: int
    number: int
    offense_team_id: int
    start_sec: float
    end_sec: float
    start_score_margin: int
    start_type: str | None = None
    has_timeout: bool
    previous_has_timeout: bool
    end_type: str
    counted: bool


class PossessionLineupRow(Row):
    """One five NBA.com credits with a possession, on offense or defense (`possession_lineup`).

    A possession a substitution splits has a row for each team's five on either side of it; one
    NBA.com does not count at all has none. Reconciliation sums these (NEU-1900).
    """

    game_id: GameId
    period: int
    number: int
    team_id: int
    lineup_key: str


class ShotRow(Row):
    """One field-goal attempt from `shotchartdetail` (`shot`).

    `event_num` is the shot's play-by-play event number, its natural key within the game.
    `clock_sec` is the game clock when it was taken: seconds left in the period. `x` and `y` are
    NBA.com's court coordinates, in tenths of a foot from the basket. `shot_type` is the kind of
    shot (`Jump Shot`, `Driving Layup Shot`); whether it was a two or a three is in `zone`.
    """

    game_id: GameId
    event_num: int
    player_id: int
    team_id: int
    period: int
    clock_sec: int
    x: int
    y: int
    distance_ft: int
    zone: ShotZone
    shot_type: str
    made: bool
    assisted_by: int | None = None
    shot_clock_bucket: str | None = None
