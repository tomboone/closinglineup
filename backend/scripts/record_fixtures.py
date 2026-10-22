"""Record trimmed live NBA.com responses into tests/fixtures/ (spec §11).

    uv run scripts/record_fixtures.py [--game-id GAME_ID] [RECORDER ...]

Run it from `backend/` on a residential IP: kumadrome, or a machine on the same network. Each
recorder is registered by name with `@recorder` and fetches through the real source functions into
a throwaway raw cache, then writes each response, trimmed, to
`tests/fixtures/<source>/<endpoint>/<key>.json`, the same layout as the cache. A recorder replaces
the fixtures of the endpoints it records. With no names given, every registered recorder runs.
"""

import argparse
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from closinglineup.clock import SystemClock
from closinglineup.season import season_label
from closinglineup.settings import Settings
from closinglineup.sources import nba_cdn
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import make_nba_session

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

type Payload = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Options:
    # The game whose box score and play-by-play are recorded; the latest final one when unset.
    game_id: str | None


type RecordFn = Callable[[Options], None]


@dataclass(frozen=True, slots=True)
class Recorder:
    record: RecordFn
    help: str


RECORDERS: dict[str, Recorder] = {}


def recorder(name: str, help: str) -> Callable[[RecordFn], RecordFn]:
    """Register `fn` under `name`; `fn` receives the command-line options."""

    def register(fn: RecordFn) -> RecordFn:
        RECORDERS[name] = Recorder(fn, help)
        return fn

    return register


def write_fixture(source: str, endpoint: str, key: str, payload: Payload) -> None:
    """Write `payload` as the endpoint's only fixture."""
    directory = FIXTURES / source / endpoint
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.json"):
        stale.unlink()
    path = directory / f"{key}.json"
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {path.relative_to(FIXTURES.parent.parent)}")


def _settings() -> Settings:
    # The recorder never touches the database; only the NBA.com settings matter.
    return Settings(database_url="unused")


# Per-game bulk in the schedule that the `game` rows and the counted-game filter never read.
SCHEDULE_DROPPED_KEYS = frozenset({"broadcasters", "pointsLeaders"})


def _schedule_kind(game: Payload) -> tuple[str, str, str]:
    """Season type (the game ID's first three digits), label and subtype: what tells phases,
    Global Games and Cup rounds apart."""
    return game["gameId"][:3], game["gameLabel"], game["gameSubtype"]


def _trim_schedule(payload: Payload) -> Payload:
    """The first game of each kind, in their original dates; the season-wide lists dropped."""
    schedule = payload["leagueSchedule"]
    seen: set[tuple[str, str, str]] = set()
    game_dates: list[Payload] = []
    for game_date in schedule["gameDates"]:
        games: list[Payload] = []
        for game in game_date["games"]:
            if _schedule_kind(game) not in seen:
                seen.add(_schedule_kind(game))
                games.append({k: v for k, v in game.items() if k not in SCHEDULE_DROPPED_KEYS})
        if games:
            game_dates.append({**game_date, "games": games})
    trimmed = {k: v for k, v in schedule.items() if k not in {"weeks", "broadcasterList"}}
    return {**payload, "leagueSchedule": {**trimmed, "gameDates": game_dates}}


def _latest_final_game(schedule: Payload) -> str:
    finals = [
        game["gameId"]
        for game_date in schedule["leagueSchedule"]["gameDates"]
        for game in game_date["games"]
        if game["gameStatus"] == nba_cdn.GAME_FINAL
    ]
    if not finals:
        raise SystemExit("no final game in the schedule yet; pass --game-id")
    return finals[-1]


# The live play-by-play's period and game start/end markers, kept for every period.
PBP_MARKERS = frozenset({"period", "game"})


def _trim_pbp(payload: Payload) -> Payload:
    """The first period in full, and the start and end markers of every period."""
    game = payload["game"]
    actions = [a for a in game["actions"] if a["period"] == 1 or a["actionType"] in PBP_MARKERS]
    return {**payload, "game": {**game, "actions": actions}}


@recorder(
    "cdn",
    "cdn.nba.com: this season's schedule (first game of each kind), the scoreboard as of now, "
    "and the live box score and play-by-play (first period plus period markers) of --game-id",
)
def record_cdn(options: Options) -> None:
    clock = SystemClock()
    season = season_label(datetime.now(_settings().tz).date())
    with (
        tempfile.TemporaryDirectory() as data_dir,
        make_nba_session(_settings(), clock) as session,
    ):
        cache = RawCache(Path(data_dir), clock)
        schedule = nba_cdn.fetch_schedule(session, cache, season)
        scoreboard = nba_cdn.fetch_todays_scoreboard(session, cache)
        game_id = options.game_id or _latest_final_game(schedule)
        boxscore = nba_cdn.fetch_live_boxscore(session, cache, game_id)
        pbp = nba_cdn.fetch_live_pbp(session, cache, game_id)
        (scoreboard_key,) = cache.list(nba_cdn.SOURCE, nba_cdn.SCOREBOARD)
    source = nba_cdn.SOURCE
    write_fixture(source, nba_cdn.SCHEDULE, season, _trim_schedule(schedule))
    write_fixture(source, nba_cdn.SCOREBOARD, scoreboard_key, scoreboard)
    write_fixture(source, nba_cdn.BOXSCORE, game_id, boxscore)
    write_fixture(source, nba_cdn.PLAYBYPLAY, game_id, _trim_pbp(pbp))


def _registry() -> str:
    width = max(map(len, RECORDERS))
    lines = [f"  {name:<{width}}  {r.help}" for name, r in sorted(RECORDERS.items())]
    return "recorders:\n" + "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record live fixtures.",
        epilog=_registry(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--game-id", help="game for the per-game recordings (default: latest final)"
    )
    parser.add_argument(
        "recorders", nargs="*", metavar="RECORDER", help="recorders to run (default: all)"
    )
    args = parser.parse_args()
    names: list[str] = args.recorders or sorted(RECORDERS)

    unknown = [n for n in names if n not in RECORDERS]
    if unknown:
        parser.error(f"unknown recorder(s): {', '.join(unknown)}")
    options = Options(game_id=args.game_id)
    for name in names:
        RECORDERS[name].record(options)


if __name__ == "__main__":
    main()
