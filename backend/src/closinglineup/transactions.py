"""The `transactions` overlay: NBA.com's player movement into `transaction` (spec §4.4).

The `daily_overlays` job runs it at 09:00 (spec §3.3); `closinglineup overlays transactions` runs it
once. The feed is every move since 2015, so each run makes the table hold exactly the feed's rows
from the feed's earliest date on, matched on (player, date, type, description): a row NBA.com
corrects or drops is replaced or removed, and a re-run on the same day reads the day's cached feed
and writes nothing. Rows dated before the feed's earliest are kept, so a feed cut short at its old
end does not delete the history it no longer holds.
"""

from collections import Counter
from dataclasses import asdict
from datetime import date
from typing import TYPE_CHECKING, Any

from closinglineup.db.models import Base
from closinglineup.db.replace import replace_rows
from closinglineup.sources.transactions import fetch, parse

if TYPE_CHECKING:
    from closinglineup.scheduler import Jobs

TABLE = "transaction"
NATURAL_KEY = ("player_id", "date", "type", "detail")


def run(jobs: "Jobs", day: date) -> dict[str, Any]:
    """Fetch the feed for `day`, or read the copy cached for it, and store its rows."""
    with jobs.nba() as session:
        payload = fetch(session, jobs.cache, day)
    movement = parse(payload)
    if not movement.rows:
        raise ValueError("the player-movement feed names no player")
    rows = [asdict(row) for row in movement.rows]
    table = Base.metadata.tables[TABLE]
    since = min(row.date for row in movement.rows)
    with jobs.db.begin() as session:
        changed, removed = replace_rows(session, table, NATURAL_KEY, rows, table.c.date >= since)
    return {
        "since": since.isoformat(),
        "rows": len(rows),
        "changed": changed,
        "removed": removed,
        "types": dict(Counter(row.type for row in movement.rows)),
        "other": [row.detail for row in movement.rows if row.type == "other"],
        "no_player": movement.no_player,
        "duplicates": movement.duplicates,
        "unknown_teams": movement.unknown_teams,
    }
