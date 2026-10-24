"""The health rule `/api/health` and `closinglineup health` both apply (spec §8): healthy when the
database answers and the latest derived date is within `STALE_AFTER` of the latest final game's
date. The image's `HEALTHCHECK` runs the CLI, so the worker container is held to it too."""

from datetime import timedelta

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from closinglineup.api.deps import latest_derived_date
from closinglineup.db.models import Game

STALE_HOURS = 36
STALE_AFTER = timedelta(hours=STALE_HOURS)


def unhealthy_reason(engine: Engine) -> str | None:
    """Why the app is unhealthy, or None when it is. A database with no final game yet has
    nothing to derive, so it is healthy."""
    try:
        with Session(engine) as session:
            final = session.scalar(select(func.max(Game.game_date)).where(Game.status == "final"))
            derived = latest_derived_date(session)
    except OperationalError as exc:
        return f"database unreachable: {exc.orig}"
    # A schema not yet migrated, say: still a reason, not a traceback or a 500.
    except DBAPIError as exc:
        return f"database query failed: {exc.orig}"
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
