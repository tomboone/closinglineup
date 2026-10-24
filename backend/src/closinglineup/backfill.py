"""The season backfill: every counted final game of a season, fetched oldest first (spec §2
Backfill).

`fetch_season` refreshes the season's schedule from stats.nba.com, which serves any season where
cdn.nba.com serves only the current one, then fetches each counted game it lists as final through
the per-game endpoints `nightly` fetches, but `NEVER_CACHED`, which only ever fails and would add
a request per game. A game already fully cached is skipped without a request.

When NBA.com stops answering, it waits out `PAUSES` before giving up. stats.nba.com throttles a
long run by holding requests open with nothing sent (seen on kumadrome 2026-10-22, a few hundred
games in), and that lasts far longer than `NbaSession`'s seconds of backoff: without the pauses
the walk stopped every few hundred games and needed restarting by hand. Each pause is followed by
another try of the game it stopped in, and a game that gets through starts the next stop back at
the first pause.

It is safe to interrupt. The raw cache is write-once and each response lands whole or not at
all, so a run stopped by Ctrl-C, or by `NbaUnreachableError` once the pauses are used up, loses
at most the request in flight: run it again and it skips the games already cached, then resumes
the game it stopped in at its first endpoint not cached. `NbaSession` holds it to `NBA_RPS`.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta

from closinglineup.clock import Clock
from closinglineup.scheduler import (
    NEVER_CACHED,
    fetch_final_game,
    final_games_between,
    fully_cached,
)
from closinglineup.sources import nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaSession, NbaUnreachableError
from closinglineup.sources.nba_stats import EndpointResult, FetchStatus

# How long to wait before each further try of a game NBA.com stopped answering in.
PAUSES = (timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=30))


@dataclass(frozen=True, slots=True)
class SeasonGame:
    """One game of the walk, and what fetching it did; no results means it was already cached."""

    game_date: date
    game_id: str
    results: list[EndpointResult] = field(default_factory=list[EndpointResult])

    @property
    def failed(self) -> list[EndpointResult]:
        return [result for result in self.results if result.status is FetchStatus.FAILED]


def _no_report(day: date, game_id: str, wait: timedelta) -> None:
    pass


def fetch_season(
    session: NbaSession,
    cache: RawCache,
    clock: Clock,
    season: str,
    *,
    start: date | None = None,
    end: date | None = None,
    force: bool = False,
    on_pause: Callable[[date, str, timedelta], None] = _no_report,
) -> Iterator[SeasonGame]:
    """Fetch each counted final game of `season` from `start` through `end` (either open when
    None) in date order, yielding each as it is done; `force` re-fetches cached games too.

    When NBA.com stops answering in a game, calls `on_pause` and sleeps the next of `PAUSES`,
    then tries the game again.

    Raises `NbaHttpError` if the schedule cannot be fetched, and `NbaUnreachableError`, naming
    the game, if NBA.com is still not answering after the last pause.
    """
    schedule = nba_stats.fetch_schedule_league_v2(session, cache, season, force=True)
    for day, game_id in final_games_between(schedule, start, end):
        if not force and fully_cached(cache, game_id):
            yield SeasonGame(day, game_id)
            continue
        waits = iter(PAUSES)
        while True:
            try:
                results = fetch_final_game(session, cache, game_id, skip=NEVER_CACHED, force=force)
                break
            except NbaUnreachableError as exc:
                wait = next(waits, None)
                if wait is None:
                    raise NbaUnreachableError(f"game {game_id} of {day}: {exc}") from exc
                on_pause(day, game_id, wait)
                clock.sleep_until(clock.now() + wait)
        yield SeasonGame(day, game_id, results)
