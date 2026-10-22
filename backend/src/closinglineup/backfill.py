"""The season backfill: every counted final game of a season, fetched oldest first (spec §2
Backfill).

`fetch_season` refreshes the season's schedule from stats.nba.com, which serves any season where
cdn.nba.com serves only the current one, then fetches each counted game it lists as final through
the per-game endpoints `nightly` fetches, but `NEVER_CACHED`, which only ever fails and would add
a request per game. A game already fully cached is skipped without a request.

It is safe to interrupt. The raw cache is write-once and each response lands whole or not at
all, so a run stopped by Ctrl-C, or by a connection error `NbaSession` gave up retrying, loses at
most the request in flight: run it again and it skips the games already cached, then resumes the
game it stopped in at its first endpoint not cached. `NbaSession` holds it to `NBA_RPS`.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date

from closinglineup.scheduler import (
    NEVER_CACHED,
    fetch_final_game,
    final_games_between,
    fully_cached,
)
from closinglineup.sources import nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaSession
from closinglineup.sources.nba_stats import EndpointResult, FetchStatus


@dataclass(frozen=True, slots=True)
class SeasonGame:
    """One game of the walk, and what fetching it did; no results means it was already cached."""

    game_date: date
    game_id: str
    results: list[EndpointResult] = field(default_factory=list[EndpointResult])

    @property
    def failed(self) -> list[EndpointResult]:
        return [result for result in self.results if result.status is FetchStatus.FAILED]


def fetch_season(
    session: NbaSession,
    cache: RawCache,
    season: str,
    *,
    start: date | None = None,
    end: date | None = None,
    force: bool = False,
) -> Iterator[SeasonGame]:
    """Fetch each counted final game of `season` from `start` through `end` (either open when
    None) in date order, yielding each as it is done; `force` re-fetches cached games too.

    Raises `NbaHttpError` if the schedule cannot be fetched.
    """
    schedule = nba_stats.fetch_schedule_league_v2(session, cache, season, force=True)
    for day, game_id in final_games_between(schedule, start, end):
        if not force and fully_cached(cache, game_id):
            yield SeasonGame(day, game_id)
            continue
        results = fetch_final_game(session, cache, game_id, skip=NEVER_CACHED, force=force)
        yield SeasonGame(day, game_id, results)
