"""The parse step over game dates: raw cache → typed rows → parquet, as the `parse` and `nightly`
commands run it (spec §3.2).

`parse_date` reads the date's games from the season's cached schedule, runs the box score and
matchup parsers over each game whose traditional box score is cached, and rewrites the date's
parquet file for every table, whole. A game a parser cannot read is reported and left out, so one
bad payload does not hold back the rest of the date; a final game with no cached box score is
reported as missing.

`player` and `roster_membership` are season-wide, not per game: each date's files hold the
season's cached player list and team rosters as they are when the date is parsed, each roster's
memberships open from the day it was fetched (`parse.roster`). The cache keeps one roster per team
per season, so parsing an earlier date after a newer roster fetch writes that fetch's
memberships, not the ones of the date; `derive` builds the history (NEU-1869). Reference rows a
parser cannot read are reported under `REFERENCE` and left out, like a game's.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from typing import Any

from closinglineup.load.parquet import ParquetStore
from closinglineup.parse.box import parse_box
from closinglineup.parse.matchups import parse_matchups
from closinglineup.parse.roster import parse_players, parse_roster
from closinglineup.parse.rows import (
    GameRow,
    MatchupRow,
    ParseError,
    PlayerGameRow,
    PlayerRow,
    RosterMembershipRow,
    Row,
)
from closinglineup.parse.schedule import parse_schedule
from closinglineup.season import season_label, season_of_game_id
from closinglineup.sources import nba_cdn, nba_stats
from closinglineup.sources.cache import RawCache

type Payload = dict[str, Any]

# `ParseReport.failed`'s key for the season's player list and rosters.
REFERENCE = "reference"

# The ways a payload of the wrong shape surfaces: `ParseError`, pydantic's `ValidationError` and
# `JSONDecodeError` (all `ValueError`s), and a missing key or item.
PAYLOAD_ERRORS = (ValueError, LookupError, TypeError)


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


def _schedule(cache: RawCache, season: str) -> list[GameRow]:
    payload = _payload(cache, nba_cdn.SOURCE, nba_cdn.SCHEDULE, season)
    if payload is None:
        raise NotCachedError(f"no {season} schedule in the raw cache")
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


def _fetched_on(cache: RawCache, endpoint: str, key: str, tz: tzinfo) -> date:
    meta = cache.meta(nba_stats.SOURCE, endpoint, key)
    if meta is None:
        raise NotCachedError(f"{endpoint} {key} is not in the raw cache")
    return datetime.fromisoformat(meta["fetched_at"]).astimezone(tz).date()


def _reference_rows(
    cache: RawCache, season: str, tz: tzinfo
) -> tuple[list[PlayerRow], list[RosterMembershipRow]]:
    """The season's players and roster memberships, from whatever of its player list and team
    rosters are cached."""
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
        for membership in parse_roster(
            roster, _fetched_on(cache, nba_stats.COMMON_TEAM_ROSTER, key, tz)
        )
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
    }
    for table, rows in tables.items():
        store.write_rows(table, day, rows)
        report.rows[table] = len(rows)
    return report
