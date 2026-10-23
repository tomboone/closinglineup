"""The `injuries` overlay: the latest official injury report into `injury_status` (spec §4.3).

The `injuries` job runs it four times a day (spec §3.3); `closinglineup overlays injuries` runs it
once. Each line of the report becomes a row with `source='pdf'`, its player and team resolved to
NBA.com IDs and its game to a `game` row where one is loaded. A line whose player cannot be
resolved is logged and listed in the run's detail, and has no row.
"""

import logging
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, insert, or_, select
from sqlalchemy.orm import Session

from closinglineup.db.models import Game, InjuryStatus, Player, RosterMembership
from closinglineup.names import NameIndex, first_last, load_aliases
from closinglineup.sources.injury_pdf import InjuryReport, discover, parse
from closinglineup.teams import by_tricode

if TYPE_CHECKING:
    from closinglineup.scheduler import Jobs

PDF_SOURCE = "pdf"

# (game date, away team ID, home team ID) → game ID.
type GameKeys = Mapping[tuple[date, int, int], str]
# (team ID, date) → the players on the team's roster that date.
type Rosters = Mapping[tuple[int, date], Collection[int]]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Resolved:
    # `injury_status` rows, as column values.
    rows: list[dict[str, Any]]
    # Each line whose player could not be resolved, as "Last, First (TEAM YYYY-MM-DD)".
    unresolved: list[str]
    # Each matchup with no `game` row, as "AWY@HOM YYYY-MM-DD".
    unmatched_games: list[str]


def resolve_rows(
    report: InjuryReport, index: NameIndex, games: GameKeys, rosters: Rosters
) -> Resolved:
    """The report's lines as `injury_status` rows, with IDs for their player, team and game.

    A player named twice for one game date keeps his first line; the second is logged.
    """
    rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    unmatched: set[str] = set()
    seen: set[tuple[int, date]] = set()
    for line in report.rows:
        team_id = by_tricode(line.team).nba_team_id
        player_id = index.resolve(
            first_last(line.player), rosters.get((team_id, line.game_date), ())
        )
        where = f"{line.player} ({line.team} {line.game_date.isoformat()})"
        if player_id is None:
            log.warning("injury report player not resolved", extra={"line": where})
            unresolved.append(where)
            continue
        if (player_id, line.game_date) in seen:
            log.warning("injury report names a player twice", extra={"line": where})
            continue
        seen.add((player_id, line.game_date))
        away, home = by_tricode(line.away).nba_team_id, by_tricode(line.home).nba_team_id
        game_id = games.get((line.game_date, away, home))
        if game_id is None:
            unmatched.add(f"{line.away}@{line.home} {line.game_date.isoformat()}")
        rows.append(
            {
                "report_datetime": report.report_datetime,
                "game_date": line.game_date,
                "game_id": game_id,
                "player_id": player_id,
                "team_id": team_id,
                "status": line.status,
                "reason": line.reason,
                "source": PDF_SOURCE,
            }
        )
    return Resolved(rows, unresolved, sorted(unmatched))


def run(jobs: "Jobs", day: date) -> dict[str, Any]:
    """Find, parse and store the latest report stamped on `day`, replacing any rows an earlier
    run stored from the same report, so a re-run leaves the table as it was."""
    with jobs.nba() as session:
        path = discover(day, session, jobs.cache, jobs.clock.now())
    report = parse(path)
    dates = {line.game_date for line in report.rows}
    with jobs.db.begin() as session:
        players = session.execute(select(Player.nba_player_id, Player.name))
        index = NameIndex(players, load_aliases())
        resolved = resolve_rows(report, index, _games(session, dates), _rosters(session, dates))
        session.execute(
            delete(InjuryStatus).where(
                InjuryStatus.report_datetime == report.report_datetime,
                InjuryStatus.source == PDF_SOURCE,
            )
        )
        if resolved.rows:
            session.execute(insert(InjuryStatus), resolved.rows)
    return {
        "report": report.report_datetime.isoformat(),
        "file": path.name,
        "rows": len(resolved.rows),
        "statuses": dict(Counter(row["status"] for row in resolved.rows)),
        "not_submitted": [f"{team} {when.isoformat()}" for when, team in report.not_submitted],
        "unresolved": resolved.unresolved,
        "unmatched_games": resolved.unmatched_games,
    }


def _games(session: Session, dates: Collection[date]) -> dict[tuple[date, int, int], str]:
    games = session.scalars(select(Game).where(Game.game_date.in_(dates)))
    return {(g.game_date, g.away_team_id, g.home_team_id): g.game_id for g in games}


def _rosters(session: Session, dates: Collection[date]) -> dict[tuple[int, date], set[int]]:
    if not dates:
        return {}
    spans = session.execute(
        select(
            RosterMembership.player_id,
            RosterMembership.team_id,
            RosterMembership.start_date,
            RosterMembership.end_date,
        ).where(
            RosterMembership.start_date <= max(dates),
            or_(RosterMembership.end_date.is_(None), RosterMembership.end_date >= min(dates)),
        )
    )
    rosters: dict[tuple[int, date], set[int]] = {}
    for player_id, team_id, start, end in spans:
        for day in dates:
            if start <= day and (end is None or day <= end):
                rosters.setdefault((team_id, day), set()).add(player_id)
    return rosters
