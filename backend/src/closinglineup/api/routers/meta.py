"""`GET /api/meta`: the latest derived date, each job's last run, how fresh each source is, and
which teams' injury reports are ESPN's stale fallback (spec §4.3, §8)."""

from datetime import date, datetime
from typing import cast

from fastapi import APIRouter
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from closinglineup.api.deps import SessionDep, latest_derived_date
from closinglineup.api.schemas import (
    InjurySource,
    InjurySourceMarker,
    JobRunSummary,
    MetaResponse,
    SourceFreshness,
)
from closinglineup.db.models import (
    IMPACT_SOURCES,
    Contract,
    Game,
    ImpactSnapshot,
    InjuryStatus,
    JobRun,
    PlayerGame,
    Transaction,
)
from closinglineup.injuries import ESPN_SOURCE, PDF_SOURCE
from closinglineup.teams import by_nba_team_id

router = APIRouter()


@router.get("/api/meta")
def meta(session: SessionDep) -> MetaResponse:
    return MetaResponse(
        latest_derived_date=latest_derived_date(session),
        jobs=_jobs(session),
        freshness=_freshness(session),
        injury_sources=_injury_sources(session),
    )


def _jobs(session: Session) -> list[JobRunSummary]:
    """Each job's latest run, by job name."""
    found = session.scalars(
        select(JobRun)
        .ext(distinct_on(JobRun.job))
        .order_by(JobRun.job, JobRun.started_at.desc(), JobRun.id.desc())
    )
    return [JobRunSummary.model_validate(run, from_attributes=True) for run in found]


def _freshness(session: Session) -> list[SourceFreshness]:
    """The newest row each source has given: box scores by game date, the injury reports by report
    time, transactions, impact snapshots and contract snapshots by date."""
    box = session.scalar(
        select(func.max(Game.game_date)).where(
            Game.game_id.in_(select(PlayerGame.game_id).distinct())
        )
    )
    reports = dict(
        session.execute(
            select(InjuryStatus.source, func.max(InjuryStatus.report_datetime)).group_by(
                InjuryStatus.source
            )
        ).all()
    )
    impacts = dict(
        session.execute(
            select(ImpactSnapshot.source, func.max(ImpactSnapshot.date)).group_by(
                ImpactSnapshot.source
            )
        ).all()
    )
    latest: list[tuple[str, datetime | date | None]] = [
        ("box_score", box),
        ("injury_pdf", reports.get(PDF_SOURCE)),
        ("injury_espn", reports.get(ESPN_SOURCE)),
        ("transactions", session.scalar(select(func.max(Transaction.date)))),
        *((source, impacts.get(source)) for source in IMPACT_SOURCES),
        ("contracts", session.scalar(select(func.max(Contract.as_of_date)))),
    ]
    return [SourceFreshness(source=source, latest=value) for source, value in latest]


def _injury_sources(session: Session) -> list[InjurySourceMarker]:
    """Each team's newest injury row's source, the official report's on a tie with ESPN's, as
    `derive.availability.latest_injury_source` picks it; a team with none is left out."""
    found = session.execute(
        select(InjuryStatus.team_id, InjuryStatus.source)
        .ext(distinct_on(InjuryStatus.team_id))
        .order_by(
            InjuryStatus.team_id,
            InjuryStatus.report_datetime.desc(),
            (InjuryStatus.source == PDF_SOURCE).desc(),
        )
    ).all()
    markers = [
        InjurySourceMarker(
            tricode=by_nba_team_id(team_id).tricode,
            source=cast(InjurySource, source),
            stale=source == ESPN_SOURCE,
        )
        for team_id, source in found
    ]
    return sorted(markers, key=lambda marker: marker.tricode)
