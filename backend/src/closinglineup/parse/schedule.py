"""cdn.nba.com's season schedule → `GameRow`s (spec §4.2, §6.1 `game`).

Only counted games become rows (`season.phase`), and only once both teams are known: the schedule
lists Cup knockout and postseason games with team ID 0 until their matchups are set, and a later
refresh fills them in.

A team is on a back-to-back when it played a counted game the calendar day before, on the
Eastern-time dates NBA.com keeps games on (`gameDateEst`); a postponed game is not one it played.
A game in progress is still `scheduled`: `game.status` has no in-progress value, and `nightly`
only loads finals.

A game's tip-off is its `gameDateTimeEst`, Eastern wall-clock time despite the `Z` it carries, as
the instant it names (v2 spec §5.5). The schedule lists a game whose time is not set yet ("TBD") at
midnight on its date, so a midnight tip-off, like a missing one, is none.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from closinglineup import season, teams
from closinglineup.parse.rows import GameRow, GameStatus
from closinglineup.sources.injury_pdf import EASTERN

# The schedule's `gameStatus`: 1 scheduled, 2 in progress, 3 final.
GAME_FINAL = 3
# `postponedStatus` is `N` for a game on as scheduled.
NOT_POSTPONED = ("", "N")

_TEAM_IDS = frozenset(t.nba_team_id for t in teams.TEAMS)


@dataclass(frozen=True, slots=True)
class _ScheduleGame:
    game_id: str
    game_subtype: str


def _games(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for day in payload["leagueSchedule"]["gameDates"]:
        yield from day["games"]


def _status(game: dict[str, Any]) -> GameStatus:
    if game.get("postponedStatus", "N") not in NOT_POSTPONED:
        return "postponed"
    return "final" if int(game.get("gameStatus", 0)) == GAME_FINAL else "scheduled"


def _tip_time(game: dict[str, Any]) -> datetime | None:
    stated: str = game.get("gameDateTimeEst") or ""
    if not stated:
        return None
    local = datetime.fromisoformat(stated.removesuffix("Z"))
    if local.time() == time():
        return None
    return local.replace(tzinfo=EASTERN).astimezone(UTC)


def parse_schedule(payload: dict[str, Any]) -> list[GameRow]:
    """Every counted game in the schedule whose two teams are known."""
    label: str = payload["leagueSchedule"]["seasonYear"]
    kept: list[tuple[dict[str, Any], season.Phase, date, int, int]] = []
    for game in _games(payload):
        phase = season.phase(_ScheduleGame(game["gameId"], game.get("gameSubtype", "")))
        home, away = int(game["homeTeam"]["teamId"]), int(game["awayTeam"]["teamId"])
        if phase is None or home not in _TEAM_IDS or away not in _TEAM_IDS:
            continue
        kept.append((game, phase, date.fromisoformat(game["gameDateEst"][:10]), home, away))

    played = {
        (team, day)
        for game, _, day, home, away in kept
        if _status(game) != "postponed"
        for team in (home, away)
    }

    def b2b(team: int, day: date) -> bool:
        return (team, day - timedelta(days=1)) in played

    rows: list[GameRow] = []
    for game, phase, day, home, away in kept:
        status = _status(game)
        final = status == "final"
        rows.append(
            GameRow.model_validate(
                dict(
                    game_id=game["gameId"],
                    season=label,
                    game_date=day,
                    tip_time=_tip_time(game),
                    phase=phase,
                    home_team_id=home,
                    away_team_id=away,
                    status=status,
                    home_score=game["homeTeam"].get("score") if final else None,
                    away_score=game["awayTeam"].get("score") if final else None,
                    home_b2b=b2b(home, day),
                    away_b2b=b2b(away, day),
                )
            )
        )
    return rows
