"""`roster_membership` spans from a season's game participation (spec §6.1).

Rosters are snapshots, so a past season has no membership history until it is built from the
season's loaded `player_game` rows (`parse.roster.participation_spans`). It is built from the
database rather than from parquet, since a season's rows span every date's file.

A load replaces the season's participation spans, as the loader's `REPLACE_SCOPES` tables are
replaced: it upserts each span on the table's natural key, updating only one whose values
changed, and deletes the participation spans starting in the season that it no longer holds,
such as one a late-loaded earlier game moved the start of. A roster's membership on the same
player, team and start date is left as it is: the natural key has no source, and the roster's,
open-ended from that date, covers the same games.
"""

from datetime import date

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import Base, Game, PlayerGame, RunStatus
from closinglineup.load.loader import NATURAL_KEYS, LoadResult
from closinglineup.parse.roster import PARTICIPATION_SOURCE, participation_spans
from closinglineup.season import season_bounds

TABLE = "roster_membership"


def _appearances(session: Session, season: str) -> list[tuple[int, int, date]]:
    found = session.execute(
        select(PlayerGame.player_id, PlayerGame.team_id, Game.game_date)
        .join(Game, Game.game_id == PlayerGame.game_id)
        .where(Game.season == season)
    )
    return [(player_id, team_id, game_date) for player_id, team_id, game_date in found]


def _replace(session: Session, season: str) -> tuple[int, int, int]:
    spans = participation_spans(_appearances(session, season))
    target = Base.metadata.tables[TABLE]
    natural = NATURAL_KEYS[TABLE]
    changed = 0
    if spans:
        stmt = insert(target).values([span.model_dump() for span in spans])
        updated = ("end_date", "contract_type", "source")
        stmt = stmt.on_conflict_do_update(
            index_elements=natural,
            set_={
                **{name: stmt.excluded[name] for name in updated},
                "updated_at": func.now(),
            },
            where=(target.c.source == PARTICIPATION_SOURCE)
            & tuple_(*(target.c[name] for name in updated)).is_distinct_from(
                tuple_(*(stmt.excluded[name] for name in updated))
            ),
        )
        changed = len(session.execute(stmt.returning(target.c.id)).all())
    first, last = season_bounds(season)
    kept = [tuple(getattr(span, name) for name in natural) for span in spans]
    stale = target.delete().where(
        target.c.source == PARTICIPATION_SOURCE,
        target.c.start_date.between(first, last),
        tuple_(*(target.c[name] for name in natural)).not_in(kept),
    )
    removed = len(session.execute(stale.returning(target.c.id)).all())
    return len(spans), changed, removed


def load_participation_spans(db: sessionmaker[Session], season: str) -> LoadResult:
    """Replace `season`'s participation spans in one transaction, keyed in the result by season.

    A database error fails the load rather than raising, as `load_table` does.
    """
    try:
        with db.begin() as session:
            rows, changed, removed = _replace(session, season)
    except SQLAlchemyError as exc:
        return LoadResult(TABLE, season, RunStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
    return LoadResult(TABLE, season, RunStatus.SUCCEEDED, rows, changed, removed)
