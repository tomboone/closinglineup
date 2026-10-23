"""The `darko` overlay: darko.app's DPM leaderboard into `impact_snapshot` (spec §4.5).

The `daily_overlays` job runs it at 09:00 (spec §3.3); `closinglineup overlays darko` runs it
once. Each line of the board becomes the day's `source='darko'` row for its player: DPM as
`value`, its offensive and defensive halves as `o_value` and `d_value`, and DARKO's projected
minutes as `minutes_basis`. A player is resolved by the board's `nba_id`, NBA.com's own, which
every line has carried so far: the row is stored under it even when the `player` table lacks it
yet, as it does a player signed since the last roster refresh, and the line is listed in the run's
detail. A line with no `nba_id` resolves by name and the alias table against his team's roster on
the day; one whose player cannot be resolved is logged and listed, and has no row.

The board is always today's, so `day` must be today, in `TZ`, unless its file is already cached:
a re-run for an earlier date reads that date's file, and a first run for one raises rather than
file today's ratings under it. Each run makes the day's rows exactly the board's, so a re-run on
the same day reads the day's cached file and writes nothing. With `DARKO_ENABLED=false` (the
default until darko.app's terms are settled, spec §10) the run is a no-op: no request, no rows.
"""

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from closinglineup.impact import ImpactSnapshotRow, replace_snapshot
from closinglineup.names import NameIndex
from closinglineup.resolve import Rosters, known_players, name_index, rosters
from closinglineup.sources.darko import ENDPOINT, SOURCE, DarkoBoard, fetch, parse

if TYPE_CHECKING:
    from closinglineup.scheduler import Jobs

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Resolved:
    rows: list[ImpactSnapshotRow]
    # How many rows were resolved by the board's `nba_id`, and how many by name.
    by_id: int
    by_name: int
    # Each line whose `nba_id` the `player` table lacks, as "First Last"; it has a row.
    unknown_players: list[str]
    # Each line whose player could not be resolved, as "First Last".
    unresolved: list[str]
    # Each line naming a player already named, which has no row, as "First Last".
    duplicates: list[str]


def resolve_rows(
    board: DarkoBoard, day: date, index: NameIndex, on_rosters: Rosters, known: Collection[int]
) -> Resolved:
    """The board's lines as `day`'s `impact_snapshot` rows, each with its player's ID; `known` is
    the IDs the `player` table holds.

    A player named twice keeps his first line; the second is logged and listed.
    """
    rows: list[ImpactSnapshotRow] = []
    unknown: list[str] = []
    unresolved: list[str] = []
    duplicates: list[str] = []
    by_id = by_name = 0
    seen: set[int] = set()
    for line in board.rows:
        player_id = line.nba_id
        if player_id is None:
            team = on_rosters.get((line.team_id, day), ()) if line.team_id is not None else ()
            player_id = index.resolve(line.player, team)
        if player_id is None:
            log.warning("DARKO player not resolved", extra={"line": line.player})
            unresolved.append(line.player)
            continue
        if player_id in seen:
            log.warning("DARKO names a player twice", extra={"line": line.player})
            duplicates.append(line.player)
            continue
        seen.add(player_id)
        if line.nba_id is None:
            by_name += 1
        else:
            by_id += 1
            if player_id not in known:
                log.warning("DARKO player not in the player table", extra={"line": line.player})
                unknown.append(line.player)
        rows.append(
            ImpactSnapshotRow(
                day, player_id, SOURCE, line.dpm, line.o_dpm, line.d_dpm, line.x_minutes
            )
        )
    return Resolved(rows, by_id, by_name, unknown, unresolved, duplicates)


def run(jobs: "Jobs", day: date) -> dict[str, Any]:
    """Fetch today's board for `day`, or read the copy cached for it, and store its rows;
    nothing when `DARKO_ENABLED` is off."""
    if not jobs.settings.darko_enabled:
        return {"enabled": False, "rows": 0}
    today = jobs.clock.now().astimezone(jobs.settings.tz).date()
    if day != today and not jobs.cache.exists(SOURCE, ENDPOINT, day.isoformat()):
        raise ValueError(
            f"the board is today's, {today.isoformat()}; {day.isoformat()} is not cached"
        )
    with jobs.http() as client:
        path = fetch(client, jobs.cache, day)
    board = parse(path)
    if not board.rows:
        raise ValueError(f"{path.name} lists no player with a DPM")
    with jobs.db.begin() as session:
        resolved = resolve_rows(
            board, day, name_index(session), rosters(session, [day]), known_players(session)
        )
        changed, removed = replace_snapshot(session, day, SOURCE, resolved.rows)
    return {
        "enabled": True,
        "file": path.name,
        "ratings_through": board.ratings_through.isoformat() if board.ratings_through else None,
        "season": board.season,
        "rows": len(resolved.rows),
        "by_id": resolved.by_id,
        "by_name": resolved.by_name,
        "changed": changed,
        "removed": removed,
        "no_value": board.no_value,
        "unknown_players": resolved.unknown_players,
        "unresolved": resolved.unresolved,
        "duplicates": resolved.duplicates,
    }
