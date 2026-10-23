"""Availability, a player's state on a date (spec §7.5), is derived here. So far this holds only
which source a team's latest injury report came from, which `/api/meta` reads for the stale marker
(spec §4.3, §8)."""

from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from closinglineup.db.models import InjuryStatus
from closinglineup.injuries import PDF_SOURCE
from closinglineup.sources.injury_pdf import EASTERN


def latest_injury_source(session: Session, team_id: int, day: date) -> str | None:
    """The `source` of the team's newest `injury_status` row reported by the end of `day`, in
    Eastern time: `espn` only when no official report row for the team is as new; None if the team
    has none."""
    end = datetime.combine(day + timedelta(days=1), time(), EASTERN)
    return session.scalars(
        select(InjuryStatus.source)
        .where(InjuryStatus.team_id == team_id, InjuryStatus.report_datetime < end)
        .order_by(InjuryStatus.report_datetime.desc(), (InjuryStatus.source == PDF_SOURCE).desc())
        .limit(1)
    ).first()
