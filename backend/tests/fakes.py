"""Stand-ins for curl_cffi, so sources can be tested without the network."""

from curl_cffi import requests


def fake_response(status: int, body: bytes = b"") -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.content = body
    return response


class ScriptedGet:
    """Stands in for `curl_cffi.Session.get`: returns or raises each scripted outcome in turn."""

    def __init__(self, *outcomes: requests.Response | Exception) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def __call__(self, url: str, params: dict[str, str] | None = None) -> requests.Response:
        self.calls.append((url, params))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
