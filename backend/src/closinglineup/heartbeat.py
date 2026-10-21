"""The healthchecks.io client: one check per job family, `/fail` when a job fails (spec §3.3)."""

import logging
from collections.abc import Mapping
from typing import Literal, Self

import httpx

from closinglineup.settings import Settings

type JobFamily = Literal["nightly", "daily", "weekly"]

TIMEOUT_SECONDS = 10.0

log = logging.getLogger(__name__)


class Heartbeat:
    """Pings each job family's check URL; a family whose URL is unset or missing is a no-op.

    A ping that cannot be delivered is logged and swallowed: monitoring must never be the reason
    a job fails or the worker stops. URLs are never logged, since anyone holding one can ping
    the check.
    """

    def __init__(self, urls: Mapping[JobFamily, str | None], client: httpx.Client) -> None:
        self._urls = {family: url.rstrip("/") for family, url in urls.items() if url}
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings, client: httpx.Client) -> Self:
        return cls(
            {
                "nightly": settings.healthchecks_url_nightly,
                "daily": settings.healthchecks_url_daily,
                "weekly": settings.healthchecks_url_weekly,
            },
            client,
        )

    def ping(self, family: JobFamily, *, fail: bool = False, reason: str = "") -> None:
        """GET the family's URL, or POST `reason` to its `/fail`, which healthchecks.io shows
        beside the ping."""
        url = self._urls.get(family)
        ping = "fail" if fail else "ok"
        if url is None:
            log.debug("heartbeat disabled", extra={"family": family, "ping": ping})
            return
        try:
            if fail:
                response = self._client.post(f"{url}/fail", content=reason, timeout=TIMEOUT_SECONDS)
            else:
                response = self._client.get(url, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
        # `InvalidURL` is not an `HTTPError`; a malformed check URL must not crash a job.
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # A status error's message quotes the URL, so log only its status code.
            error = (
                f"HTTP {exc.response.status_code}"
                if isinstance(exc, httpx.HTTPStatusError)
                else str(exc)
            )
            log.warning(
                "heartbeat ping failed", extra={"family": family, "ping": ping, "error": error}
            )
