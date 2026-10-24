"""`derive`: the derived layer over a run of game dates, each step recorded in `load_log` (spec
§3.2, §6.4).

The steps, in order, for each date in date order: garbage time on its stints
(`derive.garbage_time`), the `player_window_stats` refresh (`derive.windows`), rotation state,
events and flags (`derive.rotation`, `derive.events`, `derive.flags`), and the local BPM snapshot
(`derive.bpm`); then a reconciliation sample (`derive.reconcile`). Two depart from per-date order:

- The view is computed whole, so it is refreshed once for the run, after every date's garbage time
  (a refresh takes about 40 s over a full season, spec §6.4), and each date's `windows` row
  records that one refresh.
- Reconciliation compares season totals, the same whatever the date, so the sample is reconciled
  once, for the run's last date, as `nightly` did before it ran `derive`.

Each step upserts a (table, game date, step) `load_log` row, as `load` does with its `load` step.
A step that raises is recorded failed and stops the run: a date's events compare its rotation state
with the team's previous game date's, so deriving past a failed date would read every player there
as an arrival. `missing_dates` lists the loaded game dates whose steps have not all succeeded,
which `derive --missing` and `nightly` run again, oldest first. Deriving a date again does not
derive the later ones again, whose events read its rotation state; `nightly` never derives past an
underived date, so it leaves none behind. Reconciliation is a check, not a write, and a missed one
is not run again: a sample it cannot read (`ReconcileError`, a response not cached) is recorded as
succeeded with the reason under `skipped`, and rows outside tolerance are logged as warnings, as
before.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from typing import Any, NamedTuple

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.clock import Clock
from closinglineup.db.models import Game, LoadLog, RunStatus
from closinglineup.derive.bpm import write_bpm
from closinglineup.derive.events import SignalRule, compute_events
from closinglineup.derive.flags import FlagRule, compute_flags
from closinglineup.derive.garbage_time import GarbageTimeRule, write_garbage_time
from closinglineup.derive.reconcile import (
    ReconcileError,
    describe,
    reconcile,
    sample_teams,
    season_type_of,
)
from closinglineup.derive.rotation import TierRule, compute_rotation_state
from closinglineup.derive.windows import refresh_windows
from closinglineup.load.loader import LOAD_STEP, write_load_log
from closinglineup.season import season_label
from closinglineup.settings import Settings
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_stats import SeasonType
from closinglineup.teams import by_nba_team_id

type Detail = dict[str, Any]

log = logging.getLogger(__name__)


class Step(NamedTuple):
    """A derive step's `load_log` key beside the game date: the table it writes, and its name."""

    table: str
    name: str


GARBAGE_TIME = Step("stint", "garbage_time")
WINDOWS = Step("player_window_stats", "windows")
TIERS = Step("rotation_state", "tiers")
EVENTS = Step("rotation_event", "events")
FLAGS = Step("player_flag", "flags")
BPM = Step("impact_snapshot", "bpm")
RECONCILE = Step("stint", "reconcile")
# The steps that write, all of which a date needs to have succeeded to count as derived.
WRITES = (GARBAGE_TIME, WINDOWS, TIERS, EVENTS, FLAGS, BPM)
# A date counts as loaded, and so due a derive, once its box scores have loaded.
LOADED_TABLE = "player_game"


@dataclass(frozen=True, slots=True)
class DeriveRules:
    """Every rule `derive` applies; `from_settings` reads the garbage-time and tier thresholds
    from `Settings`, which holds no others."""

    garbage_time: GarbageTimeRule
    tiers: TierRule
    signals: SignalRule = field(default_factory=SignalRule)
    flags: FlagRule = field(default_factory=FlagRule)

    @classmethod
    def from_settings(cls, settings: Settings) -> "DeriveRules":
        return cls(GarbageTimeRule.from_settings(settings), TierRule.from_settings(settings))


@dataclass(frozen=True, slots=True)
class StepResult:
    step: Step
    day: date
    status: RunStatus
    # What the step returned, or `{"error": ...}` for one that raised.
    detail: Detail


def missing_dates(
    db: sessionmaker[Session], start: date | None = None, end: date | None = None
) -> list[date]:
    """The game dates from `start` through `end`, either open when None, whose box scores have
    loaded, with no table's last load failed, and whose derive steps have not all succeeded,
    oldest first. A date with a failed load waits for `load --missing` to heal it."""
    succeeded = LoadLog.status == RunStatus.SUCCEEDED
    failed_load = select(LoadLog.game_date).where(
        LoadLog.step == LOAD_STEP, LoadLog.status == RunStatus.FAILED
    )
    derived = (
        select(LoadLog.game_date)
        .where(tuple_(LoadLog.table_name, LoadLog.step).in_(WRITES), succeeded)
        .group_by(LoadLog.game_date)
        .having(func.count() == len(WRITES))
    )
    query = select(LoadLog.game_date).where(
        LoadLog.table_name == LOADED_TABLE,
        LoadLog.step == LOAD_STEP,
        succeeded,
        LoadLog.game_date.not_in(derived),
        LoadLog.game_date.not_in(failed_load),
    )
    if start is not None:
        query = query.where(LoadLog.game_date >= start)
    if end is not None:
        query = query.where(LoadLog.game_date <= end)
    with db() as session:
        return list(session.scalars(query.order_by(LoadLog.game_date)))


def reconcile_sample(
    db: sessionmaker[Session],
    cache: RawCache,
    day: date,
    *,
    season_type: SeasonType | None = None,
    skip: str | None = None,
) -> Detail:
    """Reconcile `reconcile.sample_teams(day)`'s stints with NBA.com's cached lineups and on/off,
    in `season_type`, by default that of the date's last final game.

    Skipped, with a warning, for a date with no final game in a season type with lineups, a
    response not cached, or `skip`, a reason the caller has, such as `nightly`'s refresh of them
    failing: a copy cached earlier covers fewer games than the stints do, so every row would read
    as outside."""
    if season_type is None:
        with db() as session:
            game_ids = session.scalars(
                select(Game.game_id)
                .where(Game.game_date == day, Game.status == "final")
                .order_by(Game.game_id)
            ).all()
        season_type = season_type_of(game_ids[-1]) if game_ids else None
    if season_type is None:
        return {"skipped": "no final game on the date in a season type with lineups"}
    teams = sample_teams(day)
    detail: Detail = {
        "season_type": season_type.slug,
        "teams": [by_nba_team_id(team_id).tricode for team_id in teams],
    }
    try:
        if skip is not None:
            raise ReconcileError(skip)
        report = reconcile(db, cache, season_label(day), season_type, teams)
    except ReconcileError as exc:
        log.warning("reconciliation skipped", extra={"reason": str(exc)})
        return {**detail, "skipped": str(exc)}
    outside = [describe(check) for check in report.outside]
    for line in outside:
        log.warning("reconciliation outside tolerance", extra={"check": line})
    return {
        **detail,
        "lineups": len(report.lineups),
        "on_court": len(report.on_court),
        "outside": outside,
    }


class _Run:
    """One run's steps, each recorded in `load_log` as it finishes; `stopped` once one fails."""

    def __init__(self, db: sessionmaker[Session], clock: Clock) -> None:
        self.db = db
        self.clock = clock
        self.results: list[StepResult] = []
        self.stopped = False

    def step(self, step: Step, days: Iterable[date], body: Callable[[], Detail]) -> None:
        """Run `body` once and record its outcome for each of `days`, unless the run has stopped."""
        if self.stopped:
            return
        started_at = self.clock.now()
        try:
            detail = body()
            status = RunStatus.SUCCEEDED
        except Exception as exc:
            log.exception("derive step failed", extra={"step": step.name})
            detail = {"error": f"{type(exc).__name__}: {exc}"}
            status = RunStatus.FAILED
            self.stopped = True
        finished_at = self.clock.now()
        with self.db.begin() as session:
            for day in days:
                write_load_log(
                    session,
                    step.table,
                    day,
                    status,
                    started_at,
                    finished_at,
                    detail,
                    step=step.name,
                )
                self.results.append(StepResult(step, day, status, detail))


def derive(
    db: sessionmaker[Session],
    cache: RawCache,
    clock: Clock,
    days: Iterable[date],
    rules: DeriveRules,
    *,
    season_type: SeasonType | None = None,
    reconcile_skip: str | None = None,
) -> list[StepResult]:
    """Run every derive step over `days`, as the module docstring describes, and return each
    step's outcome per date, in the order they ran; the run stops at the first that fails.
    `season_type` and `reconcile_skip` go to `reconcile_sample`."""
    ordered = sorted(set(days))
    if not ordered:
        return []
    run = _Run(db, clock)
    for day in ordered:
        run.step(GARBAGE_TIME, [day], partial(write_garbage_time, db, day, rules.garbage_time))
    run.step(WINDOWS, ordered, partial(refresh_windows, db))
    for day in ordered:
        run.step(TIERS, [day], partial(compute_rotation_state, db, day, rules.tiers))
        # Events compare the date's rotation state with the team's previous game date's.
        run.step(EVENTS, [day], partial(compute_events, db, day, rules.signals))
        # Flags read the date's events among their inputs.
        run.step(FLAGS, [day], partial(compute_flags, db, day, rules.flags))
        run.step(BPM, [day], partial(write_bpm, db, day))
    last = ordered[-1]
    run.step(
        RECONCILE,
        [last],
        partial(reconcile_sample, db, cache, last, season_type=season_type, skip=reconcile_skip),
    )
    return run.results


class DeriveError(Exception):
    """A derive step that failed."""


def raise_for_failures(results: Iterable[StepResult]) -> None:
    """Raise `DeriveError` naming the failed step in `results`, if one failed."""
    if failed := [r for r in results if r.status is RunStatus.FAILED]:
        listed = "; ".join(f"{r.day} {r.step.name}: {r.detail['error']}" for r in failed)
        raise DeriveError(f"derive failed: {listed}")
