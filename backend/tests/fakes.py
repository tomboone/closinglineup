"""Stand-ins for curl_cffi, so sources can be tested without the network, and for overlays."""

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from curl_cffi import requests

from closinglineup.scheduler import Jobs


def fake_response(status: int, body: bytes = b"") -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.content = body
    return response


class ScriptedGet:
    """Stands in for `curl_cffi.Session.get`: returns or raises each scripted outcome in turn."""

    def __init__(self, *outcomes: requests.Response | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def __call__(self, url: str, params: dict[str, str] | None = None) -> requests.Response:
        self.calls.append((url, params))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class RoutedGet:
    """Stands in for `curl_cffi.Session.get`: answers each URL with its routed body, and any other
    with a 404."""

    def __init__(self, routes: Mapping[str, bytes]) -> None:
        self.routes = dict(routes)
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def __call__(self, url: str, params: dict[str, str] | None = None) -> requests.Response:
        self.calls.append((url, params))
        body = self.routes.get(url)
        return fake_response(404) if body is None else fake_response(200, body)


class Connect:
    """Stands in for `NbaSession`'s `connect`: each curl session it opens answers with `get`."""

    def __init__(self, get: Callable[..., requests.Response]) -> None:
        self.get = get
        self.opened: list[requests.Session[requests.Response]] = []

    def __call__(self) -> requests.Session[requests.Response]:
        curl = requests.Session()
        curl.get = self.get  # pyright: ignore[reportAttributeAccessIssue]
        self.opened.append(curl)
        return curl


class FakeOverlay:
    """An overlay that records each run and returns `detail`, or raises `error`."""

    def __init__(
        self, error: Exception | None = None, detail: dict[str, Any] | None = None
    ) -> None:
        self.error = error
        self.detail = detail if detail is not None else {"rows": 3}
        self.calls: list[tuple[Jobs, date]] = []

    def __call__(self, jobs: Jobs, day: date) -> dict[str, Any]:
        self.calls.append((jobs, day))
        if self.error is not None:
            raise self.error
        return self.detail
