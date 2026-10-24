"""The health rule `/api/health` and `closinglineup health` both apply (spec §8): healthy when the
database answers, no response has drifted from its endpoint's shape in the last `DRIFT_HOURS`
(`sources.drift`), and the latest derived date is within `STALE_AFTER` of the latest final game's
date. The image's `HEALTHCHECK` runs the CLI, so the worker container is held to it too."""

from datetime import timedelta

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from closinglineup.api.deps import latest_derived_date
from closinglineup.db.models import Game, LoadLog, RunStatus

STALE_HOURS = 36
STALE_AFTER = timedelta(hours=STALE_HOURS)
DRIFT_HOURS = 24


def unhealthy_reason(engine: Engine) -> str | None:
    """Why the app is unhealthy, or None when it is. A database with no final game yet has
    nothing to derive, so it is healthy."""
    try:
        with Session(engine) as session:
            drifted = session.scalars(
                select(LoadLog.table_name)
                .where(
                    LoadLog.status == RunStatus.DRIFT,
                    LoadLog.finished_at > func.now() - timedelta(hours=DRIFT_HOURS),
                )
                .order_by(LoadLog.table_name)
                .distinct()
            ).all()
            final = session.scalar(select(func.max(Game.game_date)).where(Game.status == "final"))
            derived = latest_derived_date(session)
    except OperationalError as exc:
        return f"database unreachable: {exc.orig}"
    # A schema not yet migrated, say: still a reason, not a traceback or a 500.
    except DBAPIError as exc:
        return f"database query failed: {exc.orig}"
    if drifted:
        return f"schema drift in the last {DRIFT_HOURS} h: {', '.join(drifted)}"
    if final is None:
        return None
    if derived is None:
        return f"no date derived yet; the latest final game is {final}"
    if final - derived > STALE_AFTER:
        return (
            f"the latest derived date, {derived}, is more than {STALE_HOURS} h behind the latest "
            f"final game's, {final}"
        )
    return None
