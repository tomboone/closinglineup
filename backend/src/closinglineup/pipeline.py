"""The parse step over game dates: raw cache → typed rows → parquet, as the `parse` and `nightly`
commands run it (spec §3.2).

`parse_date` reads the date's games from the season's cached schedule, runs the box score and
matchup parsers over each game whose traditional box score is cached, then pbpstats over its live
play-by-play for its stints and possessions, and rewrites the date's parquet file for every
table, whole. A game a parser cannot read is reported and left out, so one bad payload does not
hold back the rest of the date; a final game with no cached box score is reported as missing. A
game whose box score parses but whose play-by-play does not (it is not cached, or pbpstats needs
an override for it) keeps its box rows and is reported as failed, with no stints or possessions.

`parse_shot_charts` parses cached shot charts into `shot` files, one per shot chart, keyed as the
shot chart is cached (`load.parquet`).

`player` and `roster_membership` are season-wide, not per game: each date's files hold the
season's cached player list and team rosters as they are when the date is parsed, each roster's
memberships open from the day it was fetched (`parse.roster`). The cache keeps one roster per team
per season, so parsing an earlier date after a newer roster fetch writes that fetch's
memberships, not the ones of the date; the history comes from the season's games once they are
loaded (`load.memberships`), and `derive` stitches it with transactions (NEU-1869). Reference rows
a parser cannot read are reported under `REFERENCE` and left out, like a game's.

A date's games come from its season's schedule, whichever cached copy was fetched last:
cdn.nba.com's, which `nightly` refreshes, or stats.nba.com's, which `fetch --season` does.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from typing import Any

from closinglineup.load.parquet import ParquetStore
from closinglineup.parse import pbp
from closinglineup.parse.box import parse_box
from closinglineup.parse.matchups import parse_matchups
from closinglineup.parse.roster import parse_players, parse_roster
from closinglineup.parse.rows import (
    GameRow,
    MatchupRow,
    ParseError,
    PlayerGameRow,
    PlayerRow,
    PossessionRow,
    RosterMembershipRow,
    Row,
    StintRow,
)
from closinglineup.parse.schedule import parse_schedule
from closinglineup.parse.shots import parse_shots
from closinglineup.season import season_bounds, season_label, season_of_game_id
from closinglineup.sources import nba_cdn, nba_stats
from closinglineup.sources.cache import RawCache

type Payload = dict[str, Any]

# `ParseReport.failed`'s key for the season's player list and rosters.
REFERENCE = "reference"

# The ways a payload of the wrong shape surfaces: `ParseError`, pydantic's `ValidationError` and
# `JSONDecodeError` (all `ValueError`s), and a missing key or item.
PAYLOAD_ERRORS = (ValueError, LookupError, TypeError)


SHOT_TABLE = "shot"


class NotCachedError(LookupError):
    """A response the parse step needs is not in the raw cache."""


@dataclass(frozen=True, slots=True)
class ParseReport:
    """What `parse_date` did for one date: the games it parsed, the final games it had no box
    score for, the games (or `REFERENCE`) a parser failed on, with why, and the rows written per
    table."""

    game_date: date
    parsed: list[str] = field(default_factory=list[str])
    missing: list[str] = field(default_factory=list[str])
    failed: dict[str, str] = field(default_factory=dict[str, str])
    rows: dict[str, int] = field(default_factory=dict[str, int])


def _payload(cache: RawCache, source: str, endpoint: str, key: str) -> Payload | None:
    raw = cache.get(source, endpoint, key)
    return None if raw is None else json.loads(raw)


# Where a season's schedule may be cached, in the same shape: cdn.nba.com's, which `nightly`
# refreshes for the current season, and stats.nba.com's, which `fetch --season` refreshes for any.
SCHEDULES = ((nba_cdn.SOURCE, nba_cdn.SCHEDULE), (nba_stats.SOURCE, nba_stats.SCHEDULE_LEAGUE_V2))


def _schedule(cache: RawCache, season: str) -> list[GameRow]:
    """The season's games, from whichever of its cached schedules was fetched last."""
    fetched = [
        (datetime.fromisoformat(meta["fetched_at"]), source, endpoint)
        for source, endpoint in SCHEDULES
        if (meta := cache.meta(source, endpoint, season)) is not None
    ]
    if not fetched:
        raise NotCachedError(f"no {season} schedule in the raw cache")
    _, source, endpoint = max(fetched)
    payload = _payload(cache, source, endpoint, season)
    assert payload is not None
    return parse_schedule(payload)


def game_date_of(cache: RawCache, game_id: str) -> date:
    """The date of `game_id`, from its season's cached schedule."""
    for game in _schedule(cache, season_of_game_id(game_id)):
        if game.game_id == game_id:
            return game.game_date
    raise NotCachedError(f"game {game_id} is not in its season's cached schedule")


def _game_rows(
    cache: RawCache, game_id: str, traditional: Payload
) -> tuple[list[PlayerGameRow], list[MatchupRow]]:
    def stats(endpoint: str) -> Payload | None:
        return _payload(cache, nba_stats.SOURCE, endpoint, game_id)

    box = parse_box(
        traditional,
        stats(nba_stats.BOXSCORE_ADVANCED_V3),
        stats(nba_stats.BOXSCORE_HUSTLE_V2),
        stats(nba_stats.BOXSCORE_USAGE_V3),
    )
    matchups_payload = stats(nba_stats.BOXSCORE_MATCHUPS_V3)
    matchups = parse_matchups(matchups_payload) if matchups_payload is not None else []
    if strays := {row.game_id for row in [*box, *matchups]} - {game_id}:
        raise ParseError(f"cached box scores of {game_id} are for game {', '.join(sorted(strays))}")
    return box, matchups


def _pbp_rows(cache: RawCache, game_id: str) -> tuple[list[StintRow], list[PossessionRow]]:
    """The game's stints and possessions, from pbpstats over its cached live play-by-play."""
    pbp.materialize(cache, game_id)
    data_dir = cache.root.parent
    return pbp.stints_for_game(data_dir, game_id), pbp.possessions_for_game(data_dir, game_id)


def _fetched_on(cache: RawCache, endpoint: str, key: str, tz: tzinfo) -> date:
    meta = cache.meta(nba_stats.SOURCE, endpoint, key)
    if meta is None:
        raise NotCachedError(f"{endpoint} {key} is not in the raw cache")
    return datetime.fromisoformat(meta["fetched_at"]).astimezone(tz).date()


def _reference_rows(
    cache: RawCache, season: str, tz: tzinfo
) -> tuple[list[PlayerRow], list[RosterMembershipRow]]:
    """The season's players and roster memberships, from whatever of its player list and team
    rosters are cached.

    A roster fetched after its season ended, as a backfill fetches last season's, still fills in
    its players, but has no memberships: it would open them on the day it was fetched, on teams
    some of its players have since left. That season's memberships come from its games.
    """
    _, season_end = season_bounds(season)
    rosters: dict[str, Payload] = {}
    for key in cache.list(nba_stats.SOURCE, nba_stats.COMMON_TEAM_ROSTER):
        if not key.endswith(f"_{season}"):
            continue
        if (
            roster := _payload(cache, nba_stats.SOURCE, nba_stats.COMMON_TEAM_ROSTER, key)
        ) is not None:
            rosters[key] = roster
    memberships = [
        membership
        for key, roster in rosters.items()
        if (as_of := _fetched_on(cache, nba_stats.COMMON_TEAM_ROSTER, key, tz)) <= season_end
        for membership in parse_roster(roster, as_of)
    ]
    all_players = _payload(cache, nba_stats.SOURCE, nba_stats.COMMON_ALL_PLAYERS, season)
    players = parse_players(all_players, rosters.values()) if all_players is not None else []
    return players, memberships


def parse_date(cache: RawCache, store: ParquetStore, day: date, tz: tzinfo) -> ParseReport:
    """Parse `day`'s games and its season's reference rows into the date's parquet files.

    `tz` dates each roster's fetch. Raises `NotCachedError` if the season's schedule is not
    cached, since without it the date has no games.
    """
    season = season_label(day)
    games = [game for game in _schedule(cache, season) if game.game_date == day]
    report = ParseReport(day)
    box_rows: list[PlayerGameRow] = []
    matchup_rows: list[MatchupRow] = []
    stint_rows: list[StintRow] = []
    possession_rows: list[PossessionRow] = []
    for game in games:
        traditional = _payload(
            cache, nba_stats.SOURCE, nba_stats.BOXSCORE_TRADITIONAL_V3, game.game_id
        )
        if traditional is None:
            if game.status == "final":
                report.missing.append(game.game_id)
            continue
        try:
            box, matchups = _game_rows(cache, game.game_id, traditional)
        except PAYLOAD_ERRORS as exc:
            report.failed[game.game_id] = f"{type(exc).__name__}: {exc}"
            continue
        box_rows.extend(box)
        matchup_rows.extend(matchups)
        report.parsed.append(game.game_id)
        try:
            stints, possessions = _pbp_rows(cache, game.game_id)
        # `PbpError`, a play-by-play that is not cached (`FileNotFoundError`), or whatever else
        # pbpstats raises on a feed it cannot read, which it does not document. It is reported.
        except Exception as exc:
            report.failed[game.game_id] = f"play-by-play: {type(exc).__name__}: {exc}"
            continue
        stint_rows.extend(stints)
        possession_rows.extend(possessions)

    players: list[PlayerRow] = []
    memberships: list[RosterMembershipRow] = []
    try:
        players, memberships = _reference_rows(cache, season, tz)
    except PAYLOAD_ERRORS as exc:
        report.failed[REFERENCE] = f"{type(exc).__name__}: {exc}"
    tables: dict[str, list[Row]] = {
        "game": [*games],
        "player": [*players],
        "roster_membership": [*memberships],
        "player_game": [*box_rows],
        "matchup": [*matchup_rows],
        "stint": [*stint_rows],
        "possession": [*possession_rows],
    }
    for table, rows in tables.items():
        store.write_rows(table, day, rows)
        report.rows[table] = len(rows)
    return report


@dataclass(frozen=True, slots=True)
class ShotParseReport:
    """What `parse_shot_charts` did: the rows written per shot chart parsed, and the shot charts
    that were not cached or a parser failed on, with why."""

    rows: dict[str, int] = field(default_factory=dict[str, int])
    failed: dict[str, str] = field(default_factory=dict[str, str])


def shot_chart_keys(cache: RawCache, season: str) -> list[str]:
    """The cache keys of every cached shot chart of `season`, of any season type."""
    return [
        key
        for key in cache.list(nba_stats.SOURCE, nba_stats.SHOT_CHART_DETAIL)
        if key.split("_")[1:2] == [season]
    ]


def parse_shot_charts(cache: RawCache, store: ParquetStore, keys: list[str]) -> ShotParseReport:
    """Parse each cached shot chart in `keys` into the `shot` file of the same key."""
    report = ShotParseReport()
    for key in keys:
        payload = _payload(cache, nba_stats.SOURCE, nba_stats.SHOT_CHART_DETAIL, key)
        if payload is None:
            report.failed[key] = "not in the raw cache"
            continue
        try:
            shots = parse_shots(payload)
        except PAYLOAD_ERRORS as exc:
            report.failed[key] = f"{type(exc).__name__}: {exc}"
            continue
        store.write_rows(SHOT_TABLE, key, shots)
        report.rows[key] = len(shots)
    return report
