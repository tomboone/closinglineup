"""Plain httpx, for the sites that do not fingerprint TLS as NBA.com does: ESPN's injuries and
darko.app. NBA.com's own requests go through `NbaSession` (`sources/nba_http.py`)."""

import httpx

TIMEOUT_SECONDS = 20.0


def make_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_SECONDS)
