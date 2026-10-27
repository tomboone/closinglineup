"""Response models for `/api` (spec §8). The frontend's `src/api/types.ts` is generated from the
OpenAPI schema they make, so every route's models are defined here at once, whichever ticket fills
its handler (NEU-1880).

Rates are fractions (0.222 for 22.2%) and ratings per 100 possessions, as `player_window_stats`'s
are; a value with nothing to compute it from is null.
"""

# `dt.date` for fields named `date`, which would otherwise shadow the type.
import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field

type Tier = Literal["injured", "starter", "core", "fringe", "out"]
type Badge = Literal["two_way", "ten_day", "exhibit_10", "g_league"]
type Availability = Literal[
    "active",
    "out",
    "doubtful",
    "questionable",
    "probable",
    "available",
    "inactive",
    "suspended",
    "g_league",
]
type InjuryStatus = Literal["Out", "Doubtful", "Questionable", "Probable", "Available"]
type InjurySource = Literal["pdf", "espn"]
type GarbageTime = Literal["include", "exclude"]
type GameStatus = Literal["scheduled", "final", "postponed"]
type Window = Literal["5", "10", "20", "season"]
type DnpReason = Literal["cd", "injury", "inactive", "suspension", "g_league", "other"]
type Flag = Literal[
    "minutes_cliff",
    "usage_up_efficiency_down",
    "bleeding",
    "earning_trust",
    "yo_yo",
    "contract_pressure",
]


class HealthResponse(BaseModel):
    """`reason` says why the app is unavailable: the database not answering, or the latest
    derived date too far behind the latest final game."""

    status: Literal["ok", "unavailable"]
    reason: str | None = None


class PlayerRef(BaseModel):
    """A player by NBA.com ID; `name` is null until the daily roster pull has his `player` row."""

    player_id: int
    name: str | None


class Point(BaseModel):
    """One game date's value in a series."""

    game_date: dt.date
    value: float | None


# `/api/meta`


class JobRunSummary(BaseModel):
    """A job's latest `job_run`."""

    job: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    status: Literal["running", "succeeded", "failed"]


class SourceFreshness(BaseModel):
    """The newest row a source has given: a report's time for the injury sources, a date for the
    rest; null when it has given none."""

    source: str
    latest: dt.datetime | dt.date | None


class InjurySourceMarker(BaseModel):
    """The source of a team's newest injury row; `stale` when it is ESPN's fallback (spec §4.3)."""

    tricode: str
    source: InjurySource
    stale: bool


class MetaResponse(BaseModel):
    latest_derived_date: dt.date | None
    jobs: list[JobRunSummary]
    freshness: list[SourceFreshness]
    injury_sources: list[InjurySourceMarker]


# `/api/teams`


class TeamSummary(BaseModel):
    """A team with its regular-season record in the season of the latest derived date, and the
    latest date it has rotation state for."""

    tricode: str
    name: str
    conference: Literal["East", "West"]
    division: str
    wins: int
    losses: int
    latest_derived_date: dt.date | None


class TeamsResponse(BaseModel):
    season: str | None
    teams: list[TeamSummary]


# `/api/teams/{tricode}`


class TierBoardRow(BaseModel):
    """A player's rotation state on the tier board's date, and three sparklines over the team's
    last 20 game dates through it: his 10-game role window's minutes share and on/off net, and his
    impact metric (`impact_source`, the first of EPM, DARKO and local BPM he has a value for)."""

    player: PlayerRef
    tier: Tier
    badges: list[Badge]
    availability: Availability
    is_short: bool
    minutes_share_10: float | None
    starter_rate_5: float | None
    played_rate_10: float | None
    minutes_share: list[Point]
    on_off_net: list[Point]
    impact: list[Point]
    impact_source: Literal["epm", "darko", "bpm_local"] | None


class ClosingLineup(BaseModel):
    """The five on the floor at the final buzzer of a game decided by 10 or fewer; `margin` is the
    team's final margin."""

    game_id: str
    game_date: dt.date
    opponent: str
    margin: int
    players: list[PlayerRef]


class LineupStats(BaseModel):
    """A five or a pair over the season through the date, and over the team's last 10 games:
    its offensive and defensive possessions and its net rating."""

    players: list[PlayerRef]
    possessions: int
    opp_possessions: int
    net_rating: float | None
    possessions_10: int
    net_rating_10: float | None


class AvailabilityRow(BaseModel):
    """A player on the team's latest injury report, or on G League assignment."""

    player: PlayerRef
    status: InjuryStatus | None
    reason: str | None
    g_league: bool


class AvailabilityBoard(BaseModel):
    """The team's newest injury report by the end of the date (null fields when it has none), and
    `stale` when that report is ESPN's fallback."""

    report_datetime: dt.datetime | None
    game_date: dt.date | None
    source: InjurySource | None
    stale: bool
    players: list[AvailabilityRow]


class RosterCounts(BaseModel):
    """Players on the roster on the date against the CBA's limits; 10-day and Exhibit 10
    contracts are standard contracts."""

    standard: int
    standard_limit: int
    two_way: int
    two_way_limit: int


class ContractNote(BaseModel):
    """A rostered player's non-guaranteed contract in the latest weekly snapshot on or before the
    date (spec §4.6). No source carries options or trade-eligible dates (NEU-1873)."""

    player: PlayerRef
    salary: int | None
    minimum_salary: bool
    guaranteed: int
    guarantee_date: dt.date
    guaranteed_after: int | None
    days_until_guarantee: int


class TeamPage(BaseModel):
    """`tier_date` is the team's latest game date on or before `date`, which the tier board and its
    sparklines are as of; null before its first."""

    tricode: str
    name: str
    date: dt.date
    tier_date: dt.date | None
    garbage_time: GarbageTime
    tier_board: list[TierBoardRow]
    closing_lineups: list[ClosingLineup]
    lineups: list[LineupStats]
    combos: list[LineupStats]
    availability: AvailabilityBoard
    roster: RosterCounts
    contracts: list[ContractNote]


# `/api/league`


class Signal(BaseModel):
    """One of a rotation event's contributing signals (spec §7.6): a numeric one's change, or the
    injury signal's `before → after` statuses or the transaction signal's type."""

    name: str
    value: float | str | None
    label: str


class Impact(BaseModel):
    """A feed row's player's latest impact value on or before its date, in the first of DARKO and
    local BPM he has one in (v2 spec §7)."""

    source: Literal["darko", "bpm_local"]
    value: float


class TierPoint(BaseModel):
    """His tier on one of his team's game dates, null on a date he has no row on."""

    game_date: dt.date
    tier: Tier | None


class RotationEventSummary(BaseModel):
    player: PlayerRef
    game_date: dt.date
    from_tier: Tier | None
    to_tier: Tier
    signals: list[Signal]
    impact: Impact | None


class TeamRotationEvents(BaseModel):
    tricode: str
    events: list[RotationEventSummary]


class Mover(BaseModel):
    """A player's 10-game value and its change over the prior 10 games; an impact mover's
    `source` names the metric, a minutes mover's is null."""

    player: PlayerRef
    tricode: str | None
    value: float | None
    delta: float
    source: Literal["epm", "darko", "bpm_local"] | None
    impact: Impact | None


class RaisedFlag(BaseModel):
    """A flag raised on the date. On `/api/league`, a yo-yo's `tiers` are his tier on each of the
    team's last 20 final game dates this season through it, the oldest first, a date he has no row
    on kept with a null tier (v2 spec §6.2); other flags', and every flag's elsewhere, are null."""

    player: PlayerRef
    tricode: str
    game_date: dt.date
    flag: Flag
    detail: dict[str, Any]
    impact: Impact | None
    tiers: list[TierPoint] | None


class InjuryChange(BaseModel):
    player: PlayerRef
    tricode: str
    from_status: InjuryStatus | None
    to_status: InjuryStatus | None
    reason: str | None
    report_datetime: dt.datetime


class TransactionSummary(BaseModel):
    player: PlayerRef
    date: dt.date
    type: str
    from_tricode: str | None
    to_tricode: str | None
    detail: str
    impact: Impact | None


class QuietSignal(BaseModel):
    """A Starter or Core player with no rotation event on the date who sat by coach's decision in
    his team's game on it, or played under 60% of his 10-game minutes per game before it (v2 spec
    §5.4)."""

    player: PlayerRef
    tricode: str
    tier: Tier
    seconds: int
    minutes_per_game_10: float
    dnp_reason: DnpReason | None
    impact: Impact | None


class LeaguePage(BaseModel):
    date: dt.date
    rotation_events: list[TeamRotationEvents]
    quiet_signals: list[QuietSignal]
    minutes_risers: list[Mover]
    minutes_fallers: list[Mover]
    flags: list[RaisedFlag]
    injury_changes: list[InjuryChange]
    transactions: list[TransactionSummary]
    impact_risers: list[Mover]
    impact_fallers: list[Mover]


# `/api/slate`


type LineChange = Literal["new", "upgraded", "downgraded", "off"]


class SlateReport(BaseModel):
    """The team's newest injury report for the game by the end of the date, `stale` when it is
    ESPN's fallback; `submitted` false, the other fields null, when it has none (v2 spec §5.1)."""

    report_datetime: dt.datetime | None
    source: InjurySource | None
    stale: bool
    submitted: bool


class SlateLine(BaseModel):
    """A player on the team's report for the game, or on G League assignment: his `status` and
    `reason` on the report (null for a G League badge alone, and for an `off` line, which carries
    its previous ones), his status on the previous report, the `change` between them (v2 spec
    §5.2), and his tier and 10-game minutes share on the team's latest derived date on or before
    last night."""

    player: PlayerRef
    status: InjuryStatus | None
    reason: str | None
    previous_status: InjuryStatus | None
    change: LineChange | None
    tier: Tier | None
    minutes_share_10: float | None
    g_league: bool
    impact: Impact | None


class ProjectedStarter(BaseModel):
    """One of the first five of the team's players by `starter_rate_5` on its latest derived date
    whose line on the report is not Out or Doubtful; `replacement` when he ranks below the top five,
    so fills the gap of one removed above him (v2 spec §5.3)."""

    player: PlayerRef
    starter_rate_5: float
    minutes_share_10: float | None
    replacement: bool
    impact: Impact | None


class SlateTeam(BaseModel):
    """One side of a slate game; `b2b` when it is the team's second night in a row."""

    tricode: str
    b2b: bool
    report: SlateReport
    lines: list[SlateLine]
    projected_starters: list[ProjectedStarter]


class SlateGame(BaseModel):
    game_id: str
    tip_time: dt.datetime | None
    home: SlateTeam
    away: SlateTeam


class SlatePage(BaseModel):
    """The date's games, by tip-off; `last_night` is the latest derived date before it, null
    before the first (v2 spec §3, §9)."""

    date: dt.date
    last_night: dt.date | None
    games: list[SlateGame]


# `/api/players/{id}`


class PlayerHeader(BaseModel):
    player: PlayerRef
    position: str | None
    tricode: str | None
    tier: Tier | None
    badges: list[Badge]


class ImpactSeries(BaseModel):
    """One impact source's latest snapshot by each game date in the window."""

    source: Literal["epm", "darko", "bpm_local"]
    points: list[Point]


class PlayerSeries(BaseModel):
    """A point per team game in the window: his minutes and share of his team's player-minutes, his
    team's net rating with him on the floor and that less its net with him off, and his TS% and
    USG%; and each impact source he has a snapshot from."""

    minutes: list[Point]
    minutes_share: list[Point]
    on_court_net: list[Point]
    on_off_net: list[Point]
    ts_pct: list[Point]
    usg_pct: list[Point]
    impact: list[ImpactSeries]


class Annotation(BaseModel):
    game_date: dt.date
    kind: Literal["injury", "transaction", "starter", "closer"]
    label: str


class ShotZone(BaseModel):
    """A zone's share of his attempts and his FG% there, over the window, the season and the prior
    season."""

    zone: Literal["rim", "short_mid", "long_mid", "corner_3", "above_break_3"]
    freq: float | None
    fg_pct: float | None
    season_freq: float | None
    season_fg_pct: float | None
    prior_freq: float | None
    prior_fg_pct: float | None


type ProductionMetric = Literal[
    "ts_pct",
    "efg_pct",
    "usg_pct",
    "ast_pct",
    "tov_pct",
    "oreb_pct",
    "dreb_pct",
    "ftr",
    "fg3a_rate",
    "pts_per36",
    "reb_per36",
    "ast_per36",
    "game_score",
    "ws",
    "vorp",
]
# How a metric reads: a fraction as a percentage, a ratio to three decimals (FTr, 3PAr), a number
# (per-36 rates, Win Shares, VORP), or a number per game played (Game Score).
type ProductionFormat = Literal["percent", "rate", "number", "per_game"]


class ProductionCell(BaseModel):
    """A metric over one production window: its value, that less the season window's, the games in
    the window and whether it is short. `o_value` and `d_value` are Win Shares' offensive and
    defensive parts, null for every other metric."""

    value: float | None
    delta_season: float | None
    games: int
    is_short: bool
    o_value: float | None
    d_value: float | None


class ProductionWindows(BaseModel):
    """A metric's cell in each production window, null where he has no such window or the metric
    is not kept for it."""

    last_5: ProductionCell | None = Field(alias="5")
    last_10: ProductionCell | None = Field(alias="10")
    last_20: ProductionCell | None = Field(alias="20")
    season: ProductionCell | None
    prior_season: ProductionCell | None


class ProductionRow(BaseModel):
    metric: ProductionMetric
    label: str
    format: ProductionFormat
    windows: ProductionWindows


class Teammate(BaseModel):
    player: PlayerRef
    possessions: int
    net_rating: float | None


class MatchupRow(BaseModel):
    """The offense of one side of a matchup against the other, summed over the window's games."""

    player: PlayerRef
    partial_poss: float
    pts: int
    fgm: int
    fga: int
    fg3m: int
    fg3a: int
    tov: int
    ast: int


class Matchups(BaseModel):
    """The defenders who guarded him, with his offense against each, and the players he guarded,
    with theirs against him; the most partial possessions first."""

    defenders: list[MatchupRow]
    guarded: list[MatchupRow]


class PlayerPage(BaseModel):
    date: dt.date
    window: Window
    garbage_time: GarbageTime
    header: PlayerHeader
    series: PlayerSeries
    annotations: list[Annotation]
    shot_profile: list[ShotZone]
    production: list[ProductionRow]
    teammates: list[Teammate]
    matchups: Matchups
    flags: list[RaisedFlag]


# `/api/games/{game_id}`


class PlayerStint(BaseModel):
    """A player's part in one of his team's stints, so the timeline draws a row per player;
    `start_sec` and `end_sec` are seconds into the period."""

    player_id: int
    team_id: int
    period: int
    start_sec: float
    end_sec: float
    is_garbage_time: bool | None


class GamePlayer(BaseModel):
    """A player who played in the game, from his box-score line, to label his timeline row."""

    player: PlayerRef
    is_starter: bool
    seconds: int


class GameLineup(BaseModel):
    """A five's results in the game: its offensive and defensive possessions, the points scored
    and allowed on them, and its net rating."""

    players: list[PlayerRef]
    possessions: int
    opp_possessions: int
    pts_for: int
    pts_against: int
    net_rating: float | None


class GameTeam(BaseModel):
    """`closing` is the five on the floor at the final buzzer, null unless the game was decided
    by 10 or fewer."""

    team_id: int
    tricode: str
    score: int | None
    players: list[GamePlayer]
    lineups: list[GameLineup]
    closing: list[PlayerRef] | None


class GarbageTimeMarker(BaseModel):
    """Where garbage time began: the first garbage-time stint's start."""

    period: int
    start_sec: float


class GamePage(BaseModel):
    """`garbage_time_from` is null when the game had no garbage time."""

    game_id: str
    game_date: dt.date
    status: GameStatus
    garbage_time: GarbageTime
    home: GameTeam
    away: GameTeam
    stints: list[PlayerStint]
    garbage_time_from: GarbageTimeMarker | None
