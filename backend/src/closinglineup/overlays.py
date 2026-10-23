"""The overlay registry: each source that writes a snapshot table (spec §6.3) registers the
callable that runs it, under the job family that schedules it (spec §3.3).

An overlay takes the worker's `Jobs`, for its settings, clock, raw cache, NBA.com sessions and
database, and the date it runs for, and returns the detail its run records. Each runs in
isolation: one that raises is recorded and the job's other overlays still run.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

from closinglineup import darko, injuries, transactions

if TYPE_CHECKING:
    from closinglineup.scheduler import Jobs

# `injuries` runs four times a day; `daily` is the `daily_overlays` job; `weekly` runs after
# the shot charts in the `weekly` job.
type OverlayFamily = Literal["injuries", "daily", "weekly"]
type OverlayFn = Callable[[Jobs, date], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class Overlay:
    name: str
    family: OverlayFamily
    run: OverlayFn


class UnknownOverlayError(LookupError):
    pass


# By name, in registration order, which is the order a job runs them in.
REGISTRY: dict[str, Overlay] = {}


def register_overlay(name: str, family: OverlayFamily, fn: OverlayFn) -> None:
    """Register `fn` as overlay `name`, for its family's job and `closinglineup overlays name`."""
    if name in REGISTRY:
        raise ValueError(f"overlay {name!r} is already registered")
    REGISTRY[name] = Overlay(name, family, fn)


def overlays_in(family: OverlayFamily) -> list[Overlay]:
    return [overlay for overlay in REGISTRY.values() if overlay.family == family]


def by_name(name: str) -> Overlay:
    try:
        return REGISTRY[name]
    except KeyError:
        known = ", ".join(REGISTRY) or "none"
        raise UnknownOverlayError(f"no overlay named {name!r}; registered: {known}") from None


register_overlay("injuries", "injuries", injuries.run)
register_overlay("transactions", "daily", transactions.run)
register_overlay("darko", "daily", darko.run)
