import logging
from collections.abc import Iterator

import httpx
import pytest
import respx

from closinglineup.heartbeat import TIMEOUT_SECONDS, Heartbeat, JobFamily
from closinglineup.settings import Settings

NIGHTLY = "https://hc-ping.com/nightly-123"
DAILY = "https://hc-ping.com/daily-456"
URLS: dict[JobFamily, str | None] = {"nightly": NIGHTLY, "daily": DAILY, "weekly": None}


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with httpx.Client() as client:
        yield client


@respx.mock
def test_ping_gets_the_familys_url(client: httpx.Client) -> None:
    nightly = respx.get(NIGHTLY).respond(200, text="OK")
    daily = respx.get(DAILY).respond(200, text="OK")

    Heartbeat(URLS, client).ping("nightly")

    assert nightly.call_count == 1
    assert daily.call_count == 0


@respx.mock
def test_fail_posts_the_reason_to_the_fail_url(client: httpx.Client) -> None:
    route = respx.post(f"{DAILY}/fail").respond(200, text="OK")

    Heartbeat(URLS, client).ping("daily", fail=True, reason="RuntimeError: boom")

    assert route.call_count == 1
    assert route.calls.last.request.content == b"RuntimeError: boom"


@respx.mock
def test_trailing_slash_on_the_url_is_ignored(client: httpx.Client) -> None:
    route = respx.post(f"{NIGHTLY}/fail").respond(200, text="OK")

    Heartbeat({"nightly": f"{NIGHTLY}/"}, client).ping("nightly", fail=True)

    assert route.call_count == 1


@respx.mock
def test_pings_use_the_heartbeat_timeout(client: httpx.Client) -> None:
    route = respx.get(NIGHTLY).respond(200)

    Heartbeat(URLS, client).ping("nightly")

    timeout = route.calls.last.request.extensions["timeout"]
    assert timeout == httpx.Timeout(TIMEOUT_SECONDS).as_dict()


@pytest.mark.respx(assert_all_called=False)
def test_unset_url_sends_nothing(
    client: httpx.Client, caplog: pytest.LogCaptureFixture, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.route()

    with caplog.at_level(logging.DEBUG, logger="closinglineup.heartbeat"):
        heartbeat = Heartbeat({}, client)
        heartbeat.ping("nightly")
        heartbeat.ping("weekly", fail=True, reason="boom")

    assert route.call_count == 0
    assert caplog.messages == ["heartbeat disabled"] * 2


@respx.mock
def test_from_settings_maps_each_family_to_its_url(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/closinglineup")
    monkeypatch.setenv("HEALTHCHECKS_URL_WEEKLY", "https://hc-ping.com/weekly-789")
    route = respx.get("https://hc-ping.com/weekly-789").respond(200)

    heartbeat = Heartbeat.from_settings(Settings(), client)
    heartbeat.ping("weekly")
    heartbeat.ping("nightly")

    assert route.call_count == 1


@pytest.mark.parametrize(
    "response",
    [httpx.Response(404, text="not found"), httpx.ConnectError("refused")],
    ids=["error-status", "connect-error"],
)
@respx.mock
def test_ping_failures_are_logged_not_raised(
    client: httpx.Client,
    caplog: pytest.LogCaptureFixture,
    response: httpx.Response | Exception,
) -> None:
    respx.get(NIGHTLY).mock(side_effect=[response])

    with caplog.at_level(logging.WARNING, logger="closinglineup.heartbeat"):
        Heartbeat(URLS, client).ping("nightly")

    assert caplog.messages == ["heartbeat ping failed"]
    # Anyone holding the URL can ping the check, so it never reaches the logs.
    assert not any(NIGHTLY in str(record.__dict__) for record in caplog.records)


def test_malformed_url_is_logged_not_raised(
    client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="closinglineup.heartbeat"):
        Heartbeat({"nightly": "https://hc-ping.com/abc\x00"}, client).ping("nightly", fail=True)

    assert caplog.messages == ["heartbeat ping failed"]
