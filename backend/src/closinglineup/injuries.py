"""The `injuries` overlay: the latest official injury report into `injury_status` (spec §4.3).

The `injuries` job runs it four times a day (spec §3.3); `closinglineup overlays injuries` runs it
once. Each line of the report becomes a row with `source='pdf'`, its player and team resolved to
NBA.com IDs and its game to a `game` row where one is loaded. A line whose player cannot be
resolved is logged and listed in the run's detail, and has no row.

When the report cannot be had, because it fails to fetch or parse or holds no lines on a day with
games, ESPN's injuries are stored in its place, with `source='espn'`, no game and the run's date
as `game_date`; `derive.availability.latest_injury_source` tells a team's stale ESPN rows apart.
"""

import logging
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from closinglineup.db.models import Game, InjuryStatus
from closinglineup.names import NameIndex, first_last
from closinglineup.resolve import Rosters, name_index, rosters
from closinglineup.sources import espn_injuries
from closinglineup.sources.espn_injuries import EspnInjuries
from closinglineup.sources.injury_pdf import InjuryReport, discover, parse
from closinglineup.teams import by_tricode

if TYPE_CHECKING:
    from closinglineup.scheduler import Jobs

PDF_SOURCE = "pdf"
ESPN_SOURCE = "espn"

# (game date, away team ID, home team ID) → game ID.
type GameKeys = Mapping[tuple[date, int, int], str]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Resolved:
    # `injury_status` rows, as column values.
    rows: list[dict[str, Any]]
    # Each line whose player could not be resolved, as "Last, First (TEAM YYYY-MM-DD)".
    unresolved: list[str]
    # Each matchup with no `game` row, as "AWY@HOM YYYY-MM-DD".
    unmatched_games: list[str]
    # Each line naming a player already named for its game date, which has no row, as above.
    duplicates: list[str]


def resolve_rows(
    report: InjuryReport, index: NameIndex, games: GameKeys, rosters: Rosters
) -> Resolved:
    """The report's lines as `injury_status` rows, with IDs for their player, team and game.

    A player named twice for one game date keeps his first line; the second is logged and listed.
    """
    rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    duplicates: list[str] = []
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
            duplicates.append(where)
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
    return Resolved(rows, unresolved, sorted(unmatched), duplicates)


def resolve_espn_rows(
    report: EspnInjuries, index: NameIndex, rosters: Rosters, day: date
) -> Resolved:
    """ESPN's lines as `injury_status` rows for `day`, with IDs for their player and team.

    A player named twice keeps his first line; the second is logged and listed.
    """
    rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    duplicates: list[str] = []
    seen: set[int] = set()
    for line in report.rows:
        team_id = by_tricode(line.team).nba_team_id
        player_id = index.resolve(line.player, rosters.get((team_id, day), ()))
        where = f"{line.player} ({line.team} {day.isoformat()})"
        if player_id is None:
            log.warning("ESPN injury player not resolved", extra={"line": where})
            unresolved.append(where)
            continue
        if player_id in seen:
            log.warning("ESPN injuries name a player twice", extra={"line": where})
            duplicates.append(where)
            continue
        seen.add(player_id)
        rows.append(
            {
                "report_datetime": report.report_datetime,
                "game_date": day,
                "game_id": None,
                "player_id": player_id,
                "team_id": team_id,
                "status": line.status,
                "reason": line.reason,
                "source": ESPN_SOURCE,
            }
        )
    return Resolved(rows, unresolved, [], duplicates)


def run(jobs: "Jobs", day: date) -> dict[str, Any]:
    """Find, parse and store the latest report stamped on `day`, or ESPN's report when it cannot
    be had. Either replaces any rows an earlier run stored from the same report, so a re-run
    leaves the table as it was."""
    try:
        with jobs.nba() as session:
            path = discover(day, session, jobs.cache, jobs.clock.now())
        report = parse(path)
    # Anything: a PDF the NBA reformats can fail inside pdfplumber, not only in `parse`.
    except Exception as exc:
        log.warning("injury report not had; falling back to ESPN", exc_info=True)
        return _run_espn(jobs, day, f"{type(exc).__name__}: {exc}")
    if not report.rows and _has_games(jobs, day):
        return _run_espn(jobs, day, f"{path.name} has no lines on a day with games")
    dates = {line.game_date for line in report.rows}
    with jobs.db.begin() as session:
        resolved = resolve_rows(
            report, name_index(session), _games(session, dates), rosters(session, dates)
        )
        _replace(session, report.report_datetime, PDF_SOURCE, resolved.rows)
    return {
        "source": PDF_SOURCE,
        "report": report.report_datetime.isoformat(),
        "file": path.name,
        "rows": len(resolved.rows),
        "statuses": dict(Counter(row["status"] for row in resolved.rows)),
        "not_submitted": [f"{team} {when.isoformat()}" for when, team in report.not_submitted],
        "unresolved": resolved.unresolved,
        "unmatched_games": resolved.unmatched_games,
        "duplicates": resolved.duplicates,
    }


def _run_espn(jobs: "Jobs", day: date, pdf_error: str) -> dict[str, Any]:
    with jobs.http() as client:
        path = espn_injuries.fetch(client, jobs.cache, day)
    report = espn_injuries.parse(path)
    with jobs.db.begin() as session:
        resolved = resolve_espn_rows(report, name_index(session), rosters(session, [day]), day)
        _replace(session, report.report_datetime, ESPN_SOURCE, resolved.rows)
    return {
        "source": ESPN_SOURCE,
        "pdf_error": pdf_error,
        "report": report.report_datetime.isoformat(),
        "file": path.name,
        "rows": len(resolved.rows),
        "statuses": dict(Counter(row["status"] for row in resolved.rows)),
        "unmapped": report.unmapped,
        "unresolved": resolved.unresolved,
        "duplicates": resolved.duplicates,
    }


def _replace(
    session: Session, report_datetime: datetime, source: str, rows: list[dict[str, Any]]
) -> None:
    session.execute(
        delete(InjuryStatus).where(
            InjuryStatus.report_datetime == report_datetime, InjuryStatus.source == source
        )
    )
    if rows:
        session.execute(insert(InjuryStatus), rows)


def _has_games(jobs: "Jobs", day: date) -> bool:
    with jobs.db() as session:
        game = session.scalar(select(Game.game_id).where(Game.game_date == day).limit(1))
    return game is not None


def _games(session: Session, dates: Collection[date]) -> dict[tuple[date, int, int], str]:
    games = session.scalars(select(Game).where(Game.game_date.in_(dates)))
    return {(g.game_date, g.away_team_id, g.home_team_id): g.game_id for g in games}
