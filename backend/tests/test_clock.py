import threading
import time
from datetime import UTC, datetime, timedelta

from closinglineup.clock import Clock, FakeClock, SystemClock


def test_system_clock_is_utc_aware() -> None:
    assert SystemClock().now().tzinfo is UTC


def test_system_clock_past_target_returns_immediately() -> None:
    clock = SystemClock()
    clock.sleep_until(clock.now() - timedelta(hours=1))


def test_system_clock_stop_cuts_a_sleep_short() -> None:
    stop = threading.Event()
    stop.set()
    clock = SystemClock(stop)

    started = time.monotonic()
    clock.sleep_until(clock.now() + timedelta(hours=1))

    assert time.monotonic() - started < 1


def test_fake_clock_sleep_jumps_forward_never_back() -> None:
    start = datetime(2026, 4, 1, 12, tzinfo=UTC)
    clock = FakeClock(start)
    as_protocol: Clock = clock

    as_protocol.sleep_until(start + timedelta(minutes=30))
    assert clock.now() == start + timedelta(minutes=30)

    as_protocol.sleep_until(start)
    assert clock.now() == start + timedelta(minutes=30)
    assert clock.sleeps == [start + timedelta(minutes=30), start]


def test_fake_clock_advance() -> None:
    start = datetime(2026, 4, 1, 12, tzinfo=UTC)
    clock = FakeClock(start)

    clock.advance(timedelta(hours=2))

    assert clock.now() == start + timedelta(hours=2)
