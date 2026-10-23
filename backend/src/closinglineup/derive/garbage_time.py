"""Garbage time: which stints were played with the game decided (spec §7.4).

A stint is garbage time when, at its start, the margin is at least M points with T seconds or
fewer left in the fourth quarter, for any `(M, T)` in `GARBAGE_TIME_RULE`, and no later stint in
the game starts within 10: a comeback clears every flag before it. Within 10 is read as closer
than `COMEBACK_MARGIN`, so a later stint at exactly 10 leaves the flags standing, as the 10-point
threshold itself counts 10 as decided. Only stint starts count, so a flagged last stint stands
however the game ends. Overtime is never garbage time, and a game that reaches it was tied at the
end of the fourth, so its fourth has none either.

The margin at a stint's start is the running sum of the team's points for less against over its
earlier stints, as the ticket has it, rather than `possession.start_score_margin`: stints credit
free throws to the ten on the floor at the foul (`parse.pbp`), so at a substitution between free
throws the sum already counts the ones still to come. Both teams' views of a stint start at the
same margin, negated, so they always get the same flag.

`write_garbage_time` classifies every stint of a date's games, both teams' rows, and writes
`stint.is_garbage_time` True or False; a re-run writes the same values and changes nothing.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import groupby
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import Game, Stint
from closinglineup.parse.rows import StintRow
from closinglineup.settings import QUARTER_SECONDS, GarbageTimeThresholds, Settings

FOURTH_QUARTER = 4
# A later stint starting closer than this un-flags every stint before it.
COMEBACK_MARGIN = 10


@dataclass(frozen=True, slots=True)
class StintScore:
    """What the rule reads of a stint: when it starts and what the team scored over it."""

    period: int
    start_sec: float
    pts_for: int
    pts_against: int

    @classmethod
    def of(cls, stint: Stint | StintRow) -> "StintScore":
        return cls(stint.period, stint.start_sec, stint.pts_for, stint.pts_against)


@dataclass(frozen=True, slots=True)
class GarbageTimeRule:
    """The thresholds, `(M, T)` pairs, and the margin a later stint un-flags within."""

    thresholds: GarbageTimeThresholds
    comeback_margin: int = COMEBACK_MARGIN

    @classmethod
    def from_settings(cls, settings: Settings) -> "GarbageTimeRule":
        return cls(settings.garbage_time_rule)

    def is_decided(self, period: int, start_sec: float, margin: int) -> bool:
        """Whether a stint starting `start_sec` into `period` at `margin` meets a threshold."""
        if period != FOURTH_QUARTER:
            return False
        left = QUARTER_SECONDS - start_sec
        return any(abs(margin) >= m and left <= t for m, t in self.thresholds)


def classify(stints: Sequence[StintScore], rule: GarbageTimeRule) -> list[bool]:
    """Each of one team's stints' garbage-time flag, the stints in game order."""
    margin = 0
    decided: list[bool] = []
    last_comeback = -1
    for i, stint in enumerate(stints):
        decided.append(rule.is_decided(stint.period, stint.start_sec, margin))
        if abs(margin) < rule.comeback_margin:
            last_comeback = i
        margin += stint.pts_for - stint.pts_against
    # A flag stands only if no later stint starts closer than the comeback margin.
    return [flag and i >= last_comeback for i, flag in enumerate(decided)]


def flag_stints(session: Session, game_id: str, rule: GarbageTimeRule) -> tuple[int, int, int]:
    """Write the garbage-time flag of every stint of `game_id`, both teams' rows, and return the
    stints, those flagged, and those whose flag changed."""
    stints = session.scalars(
        select(Stint)
        .where(Stint.game_id == game_id)
        .order_by(Stint.team_id, Stint.period, Stint.start_sec)
    ).all()
    flagged = changed = 0
    for _, team_stints in groupby(stints, key=lambda stint: stint.team_id):
        ordered = list(team_stints)
        for stint, flag in zip(
            ordered, classify([StintScore.of(stint) for stint in ordered], rule), strict=True
        ):
            flagged += flag
            if stint.is_garbage_time is not flag:
                stint.is_garbage_time = flag
                changed += 1
    return len(stints), flagged, changed


def write_garbage_time(
    db: sessionmaker[Session], day: date, rule: GarbageTimeRule
) -> dict[str, Any]:
    """Flag the stints of every game on `day` and return what was written."""
    stints = flagged = changed = 0
    with db.begin() as session:
        game_ids = session.scalars(
            select(Game.game_id).where(Game.game_date == day).order_by(Game.game_id)
        ).all()
        for game_id in game_ids:
            game_stints, game_flagged, game_changed = flag_stints(session, game_id, rule)
            stints += game_stints
            flagged += game_flagged
            changed += game_changed
    return {"games": len(game_ids), "stints": stints, "flagged": flagged, "changed": changed}
