"""Availability, a player's state on a date (spec §7.5, CONTEXT.md "Availability"), and which
source a team's latest injury report came from, which `/api/meta` reads for the stale marker (spec
§4.3, §8).

`availability` reads, in order:

1. a current G League assignment (`derive.roster.current_g_league_assignment`): `g_league`;
2. the team's most recent injury report for its game on the date, reported by the end of the date
   in Eastern time: his status on it, lowercased, if it lists him. A report reported later is not
   read, nor one for another date's game. The report is the newest of the team's rows for the
   game, the official one on a tie with ESPN's, as `latest_injury_source` picks it; a player an
   earlier report listed and the newest omits is no longer listed. A line whose reason is a G
   League one, an assignment or a two-way player with the affiliate, is not an injury, and reads
   as though the report omitted him;
3. his line in the box score of the team's game on the date: `suspended` for a suspension DNP,
   `inactive` for any other but a coach's decision, a two-way player with the affiliate included,
   or for no line at all, as the box lists only active players;
4. otherwise `active`, as on a date the team does not play.

So `out` and `doubtful`, the Injured tier's statuses (`derive.rotation`), come only from an
injury report, and `g_league` only from an assignment, as the `g_league` badge does.
"""

from datetime import date, datetime, time, timedelta
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from closinglineup.db.models import Game, InjuryStatus, PlayerGame
from closinglineup.derive.roster import ON_ASSIGNMENT, current_g_league_assignment
from closinglineup.injuries import PDF_SOURCE
from closinglineup.sources.injury_pdf import EASTERN

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

# The injury report's reason for a two-way player with the team's G League affiliate.
TWO_WAY = "G League - Two-Way"
# Reasons that put a player on the report without an injury.
NOT_INJURIES = frozenset({ON_ASSIGNMENT, TWO_WAY})
_STATUSES: dict[str, Availability] = {
    "Out": "out",
    "Doubtful": "doubtful",
    "Questionable": "questionable",
    "Probable": "probable",
    "Available": "available",
}
SUSPENSION = "suspension"
COACHS_DECISION = "cd"


def _end_of(day: date) -> datetime:
    """Midnight Eastern at the start of the day after `day`."""
    return datetime.combine(day + timedelta(days=1), time(), EASTERN)


def availability(session: Session, player_id: int, team_id: int, day: date) -> Availability:
    """The player's availability for `team_id`'s game on `day`; see the module docstring."""
    if current_g_league_assignment(session, player_id, day):
        return "g_league"
    report = session.execute(
        select(InjuryStatus.report_datetime, InjuryStatus.source)
        .where(
            InjuryStatus.team_id == team_id,
            InjuryStatus.game_date == day,
            InjuryStatus.report_datetime < _end_of(day),
        )
        .order_by(InjuryStatus.report_datetime.desc(), (InjuryStatus.source == PDF_SOURCE).desc())
        .limit(1)
    ).first()
    if report is not None:
        line = session.execute(
            select(InjuryStatus.status, InjuryStatus.reason).where(
                InjuryStatus.player_id == player_id,
                InjuryStatus.team_id == team_id,
                InjuryStatus.game_date == day,
                InjuryStatus.report_datetime == report.report_datetime,
                InjuryStatus.source == report.source,
            )
        ).first()
        if line is not None and line.reason not in NOT_INJURIES:
            return _STATUSES[line.status]
    game_id = session.scalars(
        select(Game.game_id)
        .where(
            Game.game_date == day, or_(Game.home_team_id == team_id, Game.away_team_id == team_id)
        )
        .order_by(Game.game_id)
        .limit(1)
    ).first()
    if game_id is None:
        return "active"
    line = session.execute(
        select(PlayerGame.seconds, PlayerGame.dnp_reason).where(
            PlayerGame.game_id == game_id,
            PlayerGame.player_id == player_id,
            PlayerGame.team_id == team_id,
        )
    ).first()
    if line is None:
        return "inactive"
    if line.seconds > 0 or line.dnp_reason == COACHS_DECISION:
        return "active"
    return "suspended" if line.dnp_reason == SUSPENSION else "inactive"


def latest_injury_source(session: Session, team_id: int, day: date) -> str | None:
    """The `source` of the team's newest `injury_status` row reported by the end of `day`, in
    Eastern time: `espn` only when no official report row for the team is as new; None if the team
    has none."""
    return session.scalars(
        select(InjuryStatus.source)
        .where(InjuryStatus.team_id == team_id, InjuryStatus.report_datetime < _end_of(day))
        .order_by(InjuryStatus.report_datetime.desc(), (InjuryStatus.source == PDF_SOURCE).desc())
        .limit(1)
    ).first()
