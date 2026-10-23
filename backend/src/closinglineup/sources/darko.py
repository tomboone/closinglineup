"""darko.app's DPM leaderboard: every player's DARKO Plus-Minus today (spec §4.5).

Where the data is (checked 2026-10-23). darko.app is a SvelteKit site; its leaderboard's "Download
CSV" button serializes the board in the browser (`darko-dpm-leaderboard.csv`), so there is no CSV
URL to fetch. The data behind the button is the route's own data file, `/__data.json`, which the
site's client-side navigation reads: the page's load function's result, serialized by devalue.
The Shiny app at apanalytics.shinyapps.io, which published a CSV, was taken offline in June 2026
and points here. darko.app publishes no terms of use and no robots.txt (`/robots.txt` redirects to
the home page); its About page says "Can I download the data? Yes: the leaderboard, team pages,
Compare, Standings, WOWY, Lineups, Longevity and the Fantasy Lab each have a CSV download", and
signs itself "@kmedved | www.darko.app | @anpatt7".

The file's shape. `nodes[i].data` is devalue's flat list: index 0 is the root, an object or array
holds the indices of its values, and a primitive is itself; a negative index is one of devalue's
specials (undefined, a hole, NaN, ±Infinity, -0). The leaderboard's node, the one whose root has
`players`, holds `players` as a column store, `{keys: [column, …], values: [[cell, …], …]}`, plus
`seasons`, `selectedSeason`, `asOf` and `ratingsThrough` (the date the ratings run through, e.g.
`2026-10-22`). The columns of 2026-10-23, 519 rows, every one with an `nba_id` (NBA.com's player
ID; the site's player pages are `/player/<nba_id>`):

    nba_id, player_name, team_name, tm_id (NBA.com's team ID), position, age, season (DARKO's
    end year, 2027), rookie_season, career_game_num, dpm, o_dpm, d_dpm, box_dpm, on_off_dpm,
    x_minutes (projected minutes), x_pace, x_pts_100, x_ast_100, x_fg_pct, x_fg3_pct, x_ft_pct,
    sal_market_fixed, actual_salary, surplus_value, _rank, season_games

`?season=<end year>` selects an earlier season (its columns lack `ratingsThrough`); `?through=`
changes nothing. `fetch` keeps the day's file as `raw/darko/leaderboard/<date>.json`, so the
`darko` overlay re-run on the same day reads the same file, and `parse` reads one into
`DarkoRow`s, each a player's DPM with its O/D split and projected minutes.
"""

import json
import logging
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final, cast

import httpx

from closinglineup.sources.cache import RawCache
from closinglineup.teams import UnknownTeam, by_nba_team_id

# `Final`, so the overlay can pass it where an `ImpactSource` literal is wanted.
SOURCE: Final = "darko"
ENDPOINT = "leaderboard"
URL = "https://www.darko.app/__data.json"
# The columns `parse` reads; the rest are passed over.
COLUMNS = ("nba_id", "player_name", "tm_id", "season", "dpm", "o_dpm", "d_dpm", "x_minutes")

# devalue's specials, by their negative index.
_SPECIALS: dict[int, Any] = {
    -1: None,  # undefined
    -2: None,  # a hole in a sparse array
    -3: math.nan,
    -4: math.inf,
    -5: -math.inf,
    -6: -0.0,
}

log = logging.getLogger(__name__)


class DarkoError(ValueError):
    """A response that does not read as the leaderboard's data file."""


@dataclass(frozen=True, slots=True)
class DarkoRow:
    """One player's line of the board. `nba_id` is NBA.com's player ID where the board carries
    one, and `team_id` NBA.com's team ID where `tm_id` is an NBA team's."""

    nba_id: int | None
    # "First Last", e.g. "Nikola Jokic".
    player: str
    team_id: int | None
    dpm: float
    o_dpm: float
    d_dpm: float
    x_minutes: float | None


@dataclass(frozen=True, slots=True)
class DarkoBoard:
    # The date the ratings run through; None where the file does not say.
    ratings_through: date | None
    # DARKO's season, its end year (2027 for 2026-27); None where the rows do not agree on one.
    season: int | None
    rows: list[DarkoRow]
    # Lines with no DPM, as "First Last", which have no row.
    no_value: list[str]


def fetch(client: httpx.Client, cache: RawCache, day: date, *, force: bool = False) -> Path:
    """The leaderboard's data file cached under `day`, the date it is fetched for; a cached copy
    is not re-fetched unless `force`.

    Only a 200 whose body is a JSON object with `nodes` is cached; anything else raises.
    """
    key = day.isoformat()
    if not force and cache.exists(SOURCE, ENDPOINT, key):
        return cache.path(SOURCE, ENDPOINT, key)
    response = client.get(URL, follow_redirects=True)
    response.raise_for_status()
    try:
        data: Any = json.loads(response.content)
    except ValueError as exc:
        raise DarkoError(f"response from {URL} is not JSON") from exc
    if not isinstance(data, dict) or not isinstance(cast(dict[str, Any], data).get("nodes"), list):
        raise DarkoError(f"response from {URL} is not a SvelteKit data file")
    cache.put(
        SOURCE, ENDPOINT, key, response.content, url=URL, status=response.status_code, force=force
    )
    log.info("fetched", extra={"source": SOURCE, "endpoint": ENDPOINT, "key": key})
    return cache.path(SOURCE, ENDPOINT, key)


def parse(path: Path) -> DarkoBoard:
    """Every player line of the board cached at `path`, in the board's order.

    A file with no leaderboard node, a `players` store missing one of `COLUMNS`, or a line with
    no player name raises `DarkoError`.
    """
    data: dict[str, Any] = json.loads(path.read_bytes())
    root = _leaderboard(data, path.name)
    players: dict[str, Any] = root["players"]
    keys: list[str] = players.get("keys") or []
    missing = [column for column in COLUMNS if column not in keys]
    if missing:
        raise DarkoError(f"{path.name}: players lack {', '.join(missing)}")
    columns: dict[str, list[Any]] = dict(zip(keys, players.get("values") or [], strict=True))
    rows: list[DarkoRow] = []
    no_value: list[str] = []
    seasons: set[int] = set()
    for line in zip(*(columns[column] for column in COLUMNS), strict=True):
        nba_id, name, tm_id, season, dpm, o_dpm, d_dpm, x_minutes = line
        if not isinstance(name, str) or not name:
            raise DarkoError(f"{path.name}: a line has no player name")
        if isinstance(season, int):
            seasons.add(season)
        if not _is_number(dpm) or not _is_number(o_dpm) or not _is_number(d_dpm):
            no_value.append(name)
            continue
        rows.append(
            DarkoRow(
                _int_or_none(nba_id),
                name,
                _team(tm_id),
                float(dpm),
                float(o_dpm),
                float(d_dpm),
                float(x_minutes) if _is_number(x_minutes) else None,
            )
        )
    through = root.get("ratingsThrough")
    return DarkoBoard(
        date.fromisoformat(through) if isinstance(through, str) else None,
        next(iter(seasons)) if len(seasons) == 1 else None,
        rows,
        no_value,
    )


def hydrate(values: list[Any], index: int = 0) -> Any:
    """The value at `index` of devalue's flat list, its objects and arrays filled in."""
    if index < 0:
        try:
            return _SPECIALS[index]
        except KeyError:
            raise DarkoError(f"unknown devalue special {index}") from None
    value: Any = values[index]
    if isinstance(value, dict):
        entries = cast(dict[str, int], value)
        return {key: hydrate(values, i) for key, i in entries.items()}
    if isinstance(value, list):
        items = cast(list[Any], value)
        if items and isinstance(items[0], str):
            # A tagged type: Date, Set, Map, RegExp, BigInt or a null-prototype object.
            raise DarkoError(f"unsupported devalue type {items[0]!r}")
        return [hydrate(values, cast(int, i)) for i in items]
    return value


def _leaderboard(data: dict[str, Any], name: str) -> dict[str, Any]:
    nodes: list[Any] = data.get("nodes") or []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        flat: Any = cast(dict[str, Any], node).get("data")
        if not isinstance(flat, list):
            continue
        root: Any = hydrate(cast(list[Any], flat))
        if isinstance(root, dict):
            found = cast(dict[str, Any], root)
            if isinstance(found.get("players"), dict):
                return found
    raise DarkoError(f"{name} has no leaderboard node")


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _team(tm_id: Any) -> int | None:
    try:
        return by_nba_team_id(int(tm_id)).nba_team_id
    except (TypeError, ValueError, UnknownTeam):
        return None
