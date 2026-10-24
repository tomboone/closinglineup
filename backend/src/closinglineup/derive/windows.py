"""Rolling windows: `player_window_stats`, the materialized view of every spec §7.1-7.3 metric
over each player's 5, 10 and 20-game, season and prior-season windows (spec §6.4, ADR-0003). The
view's definition, and each metric's, is `db/views.sql`.

`refresh_windows` recomputes the whole view from the tables. It refreshes concurrently, so the
API reads the previous rows until the new ones are in, and a re-run over unchanged tables leaves
the same rows. A concurrent refresh matches old rows to new by the unique key, which a production
row's NULL `team_id` never matches, so those rows are rewritten on every refresh; the timing below
includes that.

JIT is off for the refresh: the view's one query has a thousand-odd expressions, and compiling
them took longer than the rest of the query (7.8 s of 19 s below).

Timing (NEU-1875), on synthetic seasons at full scale (1,230 games, 15 players a team, 40 stints
and 85 shots a team-game, daily snapshots from all three impact sources) in a 2024 MacBook Pro's
Docker Postgres 17: 10 s for one season, as a full 2025-26 backfill is, and 20 s for two; 28 s
for two with JIT on. kumadrome's is to be measured on its first full backfill.
"""

from time import perf_counter
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

VIEW = "player_window_stats"


def refresh_windows(db: sessionmaker[Session]) -> dict[str, Any]:
    """Refresh `player_window_stats` and return its rows and the seconds the refresh took."""
    started = perf_counter()
    with db.begin() as session:
        session.execute(text("SET LOCAL jit = off"))
        session.execute(text(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {VIEW}"))
        rows = session.execute(text(f"SELECT count(*) FROM {VIEW}")).scalar_one()
    return {"rows": rows, "seconds": round(perf_counter() - started, 1)}
