"""ESPN's injuries JSON: the fallback when the official report cannot be had (spec §4.3).

Ported from nba-today's ESPN client. ESPN does not fingerprint TLS as NBA.com does, so this is
plain httpx (`sources/web.py`). Every request sends `season` (ESPN's end year,
`season.espn_season`) and `seasontype=2`; ESPN ignored both when nba-today probed it on
2026-10-20, but they are sent all the same. The report is ESPN's as of the response's own
`timestamp`, which is when it was generated.

The response groups injuries by team, naming each by ESPN ID and display name only. Each injury
names the athlete's own team too, and the two can disagree: on 2026-10-20 and again on 2026-10-23
Brandon Ingram was listed under the LA Clippers (ESPN 12) with his team Toronto (28), where he
plays. So a line goes to the athlete's team, and to its group's only when the athlete has none.

`status` is display text; the 2026-10-23 fixture holds `Out` (50) and `Day-To-Day` (29). It is
mapped onto the official report's spelling (`STATUSES`); a status with no mapping is logged and
listed, and has no line. Spec §4.3 maps `GTD` too, which `status` has not been seen to carry:
ESPN's `GTD` so far is in `details.fantasyStatus`, beside `OUT` and `OFS`, which is not read.

`fetch` keeps each response in the raw cache as `raw/espn/injuries/<instant fetched, UTC>.json`, a
snapshot like today's scoreboard, and `parse` reads one.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from closinglineup.season import espn_season
from closinglineup.sources.cache import RawCache
from closinglineup.teams import UnknownTeam, by_espn_id

SOURCE = "espn"
ENDPOINT = "injuries"
URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
REGULAR_SEASON = 2
# A snapshot's key: the instant it was fetched, in UTC, e.g. `20261023T144550Z`.
KEY_FORMAT = "%Y%m%dT%H%M%SZ"
# Spec §4.3: ESPN's statuses onto the official report's.
STATUSES = {"Out": "Out", "Day-To-Day": "Questionable", "GTD": "Questionable"}
# A `details` field ESPN does not know, which the reason leaves out.
NOT_SPECIFIED = "Not Specified"

log = logging.getLogger(__name__)


class EspnInjuriesError(ValueError):
    """A response that does not read as ESPN's injury report."""


@dataclass(frozen=True, slots=True)
class EspnInjuryRow:
    """One player's line, with the team as a tricode and the name as ESPN gives it."""

    team: str
    # "First Last", e.g. "Brandon Ingram".
    player: str
    # The official report's spelling.
    status: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class EspnInjuries:
    report_datetime: datetime
    rows: list[EspnInjuryRow]
    # Each line whose status has no mapping, as "First Last (TEAM): status".
    unmapped: list[str]


def fetch(client: httpx.Client, cache: RawCache, day: date) -> Path:
    """The cached file of ESPN's report as of now, in the season `day` falls in.

    Only a 200 whose body is a JSON object is cached; anything else raises.
    """
    params = {"season": str(espn_season(day)), "seasontype": str(REGULAR_SEASON)}
    response = client.get(URL, params=params)
    response.raise_for_status()
    try:
        data = json.loads(response.content)
    except ValueError as exc:
        raise EspnInjuriesError(f"response from {URL} is not JSON") from exc
    if not isinstance(data, dict):
        raise EspnInjuriesError(f"response from {URL} is not a JSON object")
    key = cache.clock.now().astimezone(UTC).strftime(KEY_FORMAT)
    cache.put(
        SOURCE,
        ENDPOINT,
        key,
        response.content,
        url=URL,
        status=response.status_code,
        params=params,
    )
    log.info("fetched", extra={"source": SOURCE, "endpoint": ENDPOINT, "key": key})
    return cache.path(SOURCE, ENDPOINT, key)


def parse(path: Path) -> EspnInjuries:
    """Every player line of the report cached at `path`.

    A report with no `timestamp`, a line with no player name, or a team ESPN ID not in
    `closinglineup.teams` raises `EspnInjuriesError`.
    """
    data: dict[str, Any] = json.loads(path.read_bytes())
    stamp = data.get("timestamp")
    if not isinstance(stamp, str):
        raise EspnInjuriesError(f"{path.name} has no timestamp")
    rows: list[EspnInjuryRow] = []
    unmapped: list[str] = []
    for group in data.get("injuries", []):
        for injury in group.get("injuries", []):
            athlete: dict[str, Any] = injury.get("athlete") or {}
            name: str | None = athlete.get("displayName")
            if not name:
                raise EspnInjuriesError(f"{path.name}: an injury has no athlete name")
            own: dict[str, Any] = athlete.get("team") or {}
            team = _team(own.get("id") or group.get("id"), path.name)
            status = STATUSES.get(injury.get("status"))
            if status is None:
                where = f"{name} ({team}): {injury.get('status')}"
                log.warning("ESPN injury status not mapped", extra={"line": where})
                unmapped.append(where)
                continue
            rows.append(EspnInjuryRow(team, name, status, _reason(injury.get("details") or {})))
    return EspnInjuries(datetime.fromisoformat(stamp), rows, unmapped)


def _team(espn_id: Any, name: str) -> str:
    try:
        return by_espn_id(int(espn_id)).tricode
    except (TypeError, ValueError, UnknownTeam) as exc:
        raise EspnInjuriesError(f"{name}: team {espn_id!r} is not an NBA team") from exc


def _reason(details: dict[str, Any]) -> str | None:
    """`details` as a reason, e.g. "Left Foot; Fracture"; None when ESPN gives none."""
    side, kind, detail = (_specified(details.get(key)) for key in ("side", "type", "detail"))
    where = " ".join(part for part in (side, kind) if part)
    # "Concussion; Concussion" says it once.
    return "; ".join(part for part in (where, detail if detail != kind else None) if part) or None


def _specified(value: Any) -> str | None:
    return value if isinstance(value, str) and value != NOT_SPECIFIED else None
