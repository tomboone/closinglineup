"""Dependencies the `/api` routers share: a database session, and the date a request is for
(spec §8)."""

# `dt.date` for the `date` query parameter, which would otherwise shadow the type.
import datetime as dt
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import LoadLog, RunStatus
from closinglineup.derive.run import WRITES


def get_session(request: Request) -> Iterator[Session]:
    """A session from the factory `create_app` keeps on the app's state, closed after the
    request."""
    sessions: sessionmaker[Session] = request.app.state.sessions
    with sessions() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def latest_derived_date(session: Session, before: dt.date | None = None) -> dt.date | None:
    """The latest game date every derive step that writes has succeeded for, as `derive.run`
    counts a date derived, strictly before `before` when given; None before the first."""
    query = select(func.max(LoadLog.game_date))
    if before is not None:
        query = query.where(LoadLog.game_date < before)
    return session.scalar(
        query.where(
            LoadLog.game_date.in_(
                select(LoadLog.game_date)
                .where(
                    tuple_(LoadLog.table_name, LoadLog.step).in_(WRITES),
                    LoadLog.status == RunStatus.SUCCEEDED,
                )
                .group_by(LoadLog.game_date)
                .having(func.count() == len(WRITES))
            )
        )
    )


def resolve_date(
    session: SessionDep,
    date: Annotated[
        dt.date | None, Query(description="A game date; the latest derived one if omitted.")
    ] = None,
) -> dt.date:
    """`?date=`, or the latest derived date when it is omitted; 404 when nothing is derived yet."""
    if date is not None:
        return date
    latest = latest_derived_date(session)
    if latest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No date has been derived yet")
    return latest


DateDep = Annotated[dt.date, Depends(resolve_date)]
