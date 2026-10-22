"""Record trimmed live NBA.com responses into tests/fixtures/ (spec §11).

    uv run scripts/record_fixtures.py [--game-id GAME_ID] [RECORDER ...]

Run it from `backend/` on a residential IP: kumadrome, or a machine on the same network. Each
recorder is registered by name with `@recorder` and fetches through the real source functions into
a throwaway raw cache, then writes each response, trimmed where it is bulky, to
`tests/fixtures/<source>/<endpoint>/<key>.json`, the same layout as the cache. A recorder replaces
the fixtures of the endpoints it records. With no names given, every registered recorder runs.
"""

import argparse
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from closinglineup.clock import SystemClock
from closinglineup.season import season_label
from closinglineup.settings import Settings
from closinglineup.sources import nba_cdn, nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import make_nba_session
from closinglineup.teams import by_tricode

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

type Payload = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Options:
    # The game whose box scores and play-by-play are recorded; each recorder has its own default.
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


def write_fixture(
    source: str, endpoint: str, key: str, payload: Payload, *, replace: bool = True
) -> None:
    """Write `payload` as the endpoint's only fixture, or beside the others unless `replace`."""
    directory = FIXTURES / source / endpoint
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.json") if replace else ():
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


# A 2025-26 game that went to overtime, DEN at GSW on 2025-10-23, 137-131 after one overtime: the
# full game the per-game parsers, pbpstats's among them, are tested on (spec §11).
STATS_GAME_ID = "0022500006"


@recorder(
    "stats",
    "stats.nba.com: every per-game endpoint for --game-id (default: the 2025-26 overtime game "
    f"{STATS_GAME_ID}), whole; an endpoint that fails keeps its existing fixture",
)
def record_stats(options: Options) -> None:
    clock = SystemClock()
    game_id = options.game_id or STATS_GAME_ID
    with (
        tempfile.TemporaryDirectory() as data_dir,
        make_nba_session(_settings(), clock) as session,
    ):
        cache = RawCache(Path(data_dir), clock)
        for result in nba_stats.fetch_game(session, cache, game_id):
            if result.status is nba_stats.FetchStatus.FAILED:
                print(f"skipped {result.endpoint}: {result.error}")
                continue
            raw = cache.get(nba_stats.SOURCE, result.endpoint, game_id)
            assert raw is not None
            write_fixture(nba_stats.SOURCE, result.endpoint, game_id, json.loads(raw))


# The full games `parse/pbp.py` is tested on (spec §11): the overtime game above, and MIN at OKC
# on 2025-11-26, whose substitutions logged between free throws and end-of-quarter heave are the
# ordering quirks the stint builder has to handle.
PBP_GAME_IDS = (STATS_GAME_ID, "0022500064")
PBP_FIXTURES = "pbp"


@recorder(
    "pbp",
    "cdn.nba.com: the live play-by-play and box score of each pbpstats test game "
    f"({', '.join(PBP_GAME_IDS)}), whole",
)
def record_pbp(options: Options) -> None:
    clock = SystemClock()
    with (
        tempfile.TemporaryDirectory() as data_dir,
        make_nba_session(_settings(), clock) as session,
    ):
        cache = RawCache(Path(data_dir), clock)
        payloads = [
            (endpoint, game_id, fetch(session, cache, game_id))
            for game_id in PBP_GAME_IDS
            for endpoint, fetch in (
                (nba_cdn.PLAYBYPLAY, nba_cdn.fetch_live_pbp),
                (nba_cdn.BOXSCORE, nba_cdn.fetch_live_boxscore),
            )
        ]
    for endpoint, game_id, payload in payloads:
        replace = game_id == PBP_GAME_IDS[0]
        write_fixture(PBP_FIXTURES, endpoint, game_id, payload, replace=replace)


# The season-wide recordings: the fixture game's season, date and home team, and its top scorer.
SEASON = "2025-26"
SEASON_DATE = date(2025, 10, 23)
SEASON_TEAM = by_tricode("GSW").nba_team_id
SEASON_PLAYER = 201939  # Stephen Curry
# Rows kept of each result set in a trimmed fixture: enough to show the shape.
TRIMMED_ROWS = 10


def _trim_rows(payload: Payload) -> Payload:
    """The first `TRIMMED_ROWS` rows of each result set."""
    result_sets = [{**rs, "rowSet": rs["rowSet"][:TRIMMED_ROWS]} for rs in payload["resultSets"]]
    return {**payload, "resultSets": result_sets}


def _trim_shots(payload: Payload) -> Payload:
    """The shots of the per-game fixture game, so they can be checked against its box score, and
    the first `TRIMMED_ROWS` rows of the league averages."""
    shots, *rest = payload["resultSets"]
    game = shots["headers"].index("GAME_ID")
    kept = {**shots, "rowSet": [row for row in shots["rowSet"] if row[game] == STATS_GAME_ID]}
    averages = [{**rs, "rowSet": rs["rowSet"][:TRIMMED_ROWS]} for rs in rest]
    return {**payload, "resultSets": [kept, *averages]}


@recorder(
    "stats-season",
    f"stats.nba.com: every season-wide endpoint for {SEASON} (regular season), the scoreboard "
    f"of {SEASON_DATE}, and GSW and Stephen Curry where one team or player is asked for; the "
    f"league-wide lists trimmed to their first {TRIMMED_ROWS} rows, and Curry's shots to those "
    f"of {STATS_GAME_ID}",
)
def record_stats_season(options: Options) -> None:
    clock = SystemClock()
    regular = nba_stats.SeasonType.REGULAR_SEASON
    advanced = nba_stats.MeasureType.ADVANCED
    with (
        tempfile.TemporaryDirectory() as data_dir,
        make_nba_session(_settings(), clock) as session,
    ):
        cache = RawCache(Path(data_dir), clock)

        def record(
            endpoint: str, payload: Payload, *, trim: Callable[[Payload], Payload] | None = None
        ) -> None:
            (key,) = cache.list(nba_stats.SOURCE, endpoint)
            write_fixture(nba_stats.SOURCE, endpoint, key, trim(payload) if trim else payload)

        record(nba_stats.SCOREBOARD_V3, nba_stats.fetch_scoreboard_v3(session, cache, SEASON_DATE))
        record(
            nba_stats.COMMON_TEAM_ROSTER,
            nba_stats.fetch_common_team_roster(session, cache, SEASON_TEAM, SEASON),
        )
        record(
            nba_stats.COMMON_ALL_PLAYERS,
            nba_stats.fetch_common_all_players(session, cache, SEASON),
            trim=_trim_rows,
        )
        record(
            nba_stats.PLAYER_GAME_LOGS,
            nba_stats.fetch_player_game_logs(session, cache, SEASON, regular),
            trim=_trim_rows,
        )
        record(
            nba_stats.SHOT_CHART_DETAIL,
            nba_stats.fetch_shot_chart_detail(session, cache, SEASON_PLAYER, SEASON, regular),
            trim=_trim_shots,
        )
        record(
            nba_stats.LEAGUE_DASH_LINEUPS,
            nba_stats.fetch_league_dash_lineups(session, cache, SEASON, regular),
            trim=_trim_rows,
        )
        record(
            nba_stats.LEAGUE_PLAYER_ON_DETAILS,
            nba_stats.fetch_league_player_on_details(session, cache, SEASON_TEAM, SEASON, regular),
        )
        record(
            nba_stats.LEAGUE_DASH_PLAYER_STATS,
            nba_stats.fetch_league_dash_player_stats(session, cache, SEASON, regular, advanced),
            trim=_trim_rows,
        )
        record(
            nba_stats.LEAGUE_DASH_TEAM_STATS,
            nba_stats.fetch_league_dash_team_stats(session, cache, SEASON, regular, advanced),
        )


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
        "--game-id", help="game for the per-game recordings (default: see each recorder)"
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
