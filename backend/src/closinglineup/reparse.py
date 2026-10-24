"""`reparse`: parse, load and derive a run of game dates again from the raw cache alone, as after
a parser fix (spec §5).

It asks NBA.com nothing: it never opens a session. Before it writes anything it checks that each
final game of the dates has what its parse reads, the cdn.nba.com live play-by-play and either
every stats.nba.com per-game endpoint `nightly` caches or, for a game stats.nba.com failed for, the
cdn.nba.com live box in their place (spec §4.2), and refuses the run with `UncachedError`, naming
each game and what it lacks, if one does not. A date whose season's schedule is not cached is
refused with `NotCachedError`.

Then each date is parsed (`pipeline.parse_date`), loaded, the roster memberships rebuilt
(`derive.roster`) and the dates derived (`derive.run`), as `nightly` does after its fetch. `tables`
narrows the parse and the load to those tables; the derive runs whichever they are. A load that
fails stops the run before the derive, as in `nightly`. Only the run's own dates are derived: a
later date's events, which compare with the rotation state before them, are not derived again.
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, tzinfo

from sqlalchemy.orm import Session, sessionmaker

from closinglineup.clock import Clock
from closinglineup.db.models import RunStatus
from closinglineup.derive.roster import rebuild_roster_membership
from closinglineup.derive.run import DeriveRules, StepResult, derive
from closinglineup.load.loader import LoadResult, load_dates
from closinglineup.load.parquet import ParquetStore
from closinglineup.pipeline import NotCachedError, ParseReport, games_on, parse_date
from closinglineup.scheduler import stats_missing
from closinglineup.sources import nba_cdn, nba_stats
from closinglineup.sources.cache import RawCache


class UncachedError(NotCachedError):
    """Final games of a reparse's dates whose responses the raw cache lacks: `missing` maps each
    date to its games, each to what it lacks."""

    def __init__(self, missing: dict[date, dict[str, list[str]]]) -> None:
        games = sum(len(lacking) for lacking in missing.values())
        super().__init__(f"{games} final games lack cached responses")
        self.missing = missing


def _lacks(cache: RawCache, game_id: str) -> list[str]:
    """What a final game's parse reads that the cache does not hold, as `<source>/<endpoint>`."""
    lacks: list[str] = []
    stats = stats_missing(cache, game_id)
    if stats and not cache.exists(nba_cdn.SOURCE, nba_cdn.BOXSCORE, game_id):
        listed = ", ".join(f"{nba_stats.SOURCE}/{endpoint}" for endpoint in stats)
        lacks.append(f"{listed}, or {nba_cdn.SOURCE}/{nba_cdn.BOXSCORE} in their place")
    if not cache.exists(nba_cdn.SOURCE, nba_cdn.PLAYBYPLAY, game_id):
        lacks.append(f"{nba_cdn.SOURCE}/{nba_cdn.PLAYBYPLAY}")
    return lacks


def uncached_games(cache: RawCache, days: Collection[date]) -> dict[date, dict[str, list[str]]]:
    """The final games of `days` that lack a response their parse reads, by date, each with what
    it lacks; raises `NotCachedError` for a date whose season's schedule is not cached."""
    missing: dict[date, dict[str, list[str]]] = {}
    for day in sorted(days):
        for game in games_on(cache, day):
            if game.status == "final" and (lacks := _lacks(cache, game.game_id)):
                missing.setdefault(day, {})[game.game_id] = lacks
    return missing


@dataclass(frozen=True, slots=True)
class ReparseReport:
    """What `reparse` did: each date's parse, each table's load, the membership rebuild, and the
    derive steps, none if a load failed."""

    parsed: list[ParseReport]
    loaded: list[LoadResult]
    memberships: LoadResult
    derived: list[StepResult] = field(default_factory=list[StepResult])


def reparse(
    db: sessionmaker[Session],
    cache: RawCache,
    store: ParquetStore,
    clock: Clock,
    days: Collection[date],
    rules: DeriveRules,
    tz: tzinfo,
    tables: Collection[str] | None = None,
) -> ReparseReport:
    """Parse, load and derive `days` from the raw cache, the parse and load only of `tables` if
    given; `tz` dates each roster's fetch, as in `parse_date`.

    Raises `UncachedError` or `NotCachedError`, having written nothing, if a date lacks what its
    parse reads.
    """
    if missing := uncached_games(cache, days):
        raise UncachedError(missing)
    ordered = sorted(days)
    parsed = [parse_date(cache, store, day, tz, tables) for day in ordered]
    loaded = load_dates(db, store, clock, ordered, tables=tables)
    memberships = rebuild_roster_membership(db, cache, tz)
    if any(result.status is RunStatus.FAILED for result in [*loaded, memberships]):
        return ReparseReport(parsed, loaded, memberships)
    return ReparseReport(parsed, loaded, memberships, derive(db, cache, clock, ordered, rules))
