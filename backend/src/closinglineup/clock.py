"""Time as a dependency, so scheduling can be tested without waiting."""

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol

# How often a sleeping `SystemClock` checks its stop event.
POLL_SECONDS = 1.0


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant, timezone-aware."""
        ...

    def sleep_until(self, when: datetime) -> None:
        """Block until `when`; return immediately if it has already passed."""
        ...


class SystemClock:
    """Real time. Once `stop` is set, every sleep returns within `POLL_SECONDS`.

    The sleep polls `stop.is_set()` rather than calling `stop.wait()`: a SIGTERM handler sets the
    event, and `wait()` holds the lock `set()` needs, so a signal landing inside it would deadlock.
    """

    def __init__(self, stop: threading.Event | None = None) -> None:
        self._stop = stop or threading.Event()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep_until(self, when: datetime) -> None:
        while not self._stop.is_set() and (remaining := (when - self.now()).total_seconds()) > 0:
            time.sleep(min(remaining, POLL_SECONDS))


class FakeClock:
    """A Clock whose time only moves when told to; sleeping jumps straight to the target."""

    def __init__(self, start: datetime) -> None:
        self.current = start
        self.sleeps: list[datetime] = []

    def now(self) -> datetime:
        return self.current

    def sleep_until(self, when: datetime) -> None:
        self.sleeps.append(when)
        self.current = max(self.current, when)

    def advance(self, delta: timedelta) -> None:
        self.current += delta
