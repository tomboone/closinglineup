"""The pbpstats bridge: cached play-by-play → `StintRow`s and `PossessionRow`s (spec §3.1, §6.2).

pbpstats fetches with `requests`, which NBA.com blocks, so it only ever runs in file mode over a
directory this module fills from the raw cache (ADR-0002). This is the only importer of pbpstats.

The play-by-play is cdn.nba.com's live feed, which pbpstats reads through its `live` loaders.
stats.nba.com's `playbyplayv2`, which its `stats_nba` loaders read, answers `{}` for every game
(NEU-1855), so those loaders and `boxscoretraditionalv2` are not used.

File layout, as pbpstats 1.3.11 (`PBPSTATS_VERSION`) reads it under `DATA_DIR/pbpstats/`:

    pbp/live_<game_id>.json       `raw/cdn/playbyplay/<game_id>.json`, copied by `materialize`
                                  less its `game` actions (below)
    overrides/missing_period_starters.json
                                  {game_id: {period: {team_id: [five player IDs]}}}, for a period
                                  whose starters pbpstats cannot infer from its events

The live loaders read nothing else: no box score, and no other override. They do load
`possession_change_event_overrides.json` and `non_possession_changing_event_overrides.json`, but
only the `stats_nba` events consult them, so they are not committed; nor is
`bad_pbp_possessions.json`, which only the `stats_nba` possession loader reads. The override file
is committed under `backend/pbpstats_overrides/`; `materialize` copies it in beside each game it
writes, so pbpstats always reads the committed version.

Ordering quirks handled here, seen in 2025-26 feeds:

- Live substitutions are separate `in` and `out` events, so a team briefly has four or six
  players while a dead-ball swap is logged. Stints are spans of elapsed clock, credited to the ten
  on the floor while the clock ran, so those passing states never become stints.
- Substitutions are logged between free throws, and a possession can end on a substitution
  event. pbpstats credits a free throw, and a possession ending in one, to the ten on the floor
  at the foul. When that ten played no time (swapped in and out at one dead ball), its points and
  possessions go to the stint at that instant whose ten shares the most players with it.
- The 2025-26 feeds log end-of-quarter heaves as a `heave` action, which pbpstats 1.3.11 does not
  know. A possession that follows a rebound of one has no start type.
- Every feed ends with a `game`/`end` action, which pbpstats 1.3.11 does not know either. It
  would end the game's last possession with no offense, so `materialize` drops `game` actions;
  the period's end event then ends that possession, as it does every other period's.
- Substitutions between periods are logged before the period's start event, and pbpstats's
  `Possession.offense_team_id` reads the first event, so it names the wrong team for some
  period-opening possessions. A possession's offense is taken from its ending event instead,
  which is also what pbpstats's own possession counting reads.
"""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

# pbpstats ships no type stubs; `_Event` and `_Possession` type what this module reads.
from pbpstats.data_loader import (  # pyright: ignore[reportMissingTypeStubs]
    LivePossessionFileLoader,
    LivePossessionLoader,
)
from pbpstats.resources.enhanced_pbp import (  # pyright: ignore[reportMissingTypeStubs]
    FieldGoal,
    FreeThrow,
)

from closinglineup.parse.rows import PossessionRow, StintRow
from closinglineup.sources import nba_cdn
from closinglineup.sources.cache import RawCache, write_atomic

PBPSTATS_VERSION = "1.3.11"

# `backend/pbpstats_overrides/`: `parents[3]` of `backend/src/closinglineup/parse/pbp.py`, and of
# `/app/src/closinglineup/parse/pbp.py` in the image.
OVERRIDES_SOURCE = Path(__file__).resolve().parents[3] / "pbpstats_overrides"
OVERRIDE_FILES = ("missing_period_starters.json",)

# The live feed's game start and end markers, which pbpstats 1.3.11 cannot read.
GAME_ACTION = "game"
# The live feed's end-of-quarter heave, which pbpstats 1.3.11 does not know either.
HEAVE_ACTION = "heave"

REGULATION_PERIODS = 4
REGULATION_PERIOD_SECONDS = 720
OVERTIME_PERIOD_SECONDS = 300
LINEUP_SIZE = 5


class PbpError(Exception):
    """A game's play-by-play cannot be read, or turned into stints without an override."""


class _Event(Protocol):
    """The parts of a pbpstats enhanced PBP event this module reads."""

    period: int
    clock: str
    previous_event: "_Event | None"
    team_id: int
    is_made: bool
    shot_value: int
    count_as_possession: bool
    event_for_efficiency_stats: "_Event"

    @property
    def seconds_remaining(self) -> float: ...
    @property
    def seconds_since_previous_event(self) -> float: ...
    @property
    def current_players(self) -> dict[int, list[int]]: ...
    def get_offense_team_id(self) -> int: ...


class _Possession(Protocol):
    """The parts of a pbpstats possession this module reads."""

    period: int
    number: int
    events: list[_Event]
    start_time: str
    end_time: str
    start_score_margin: int
    possession_has_timeout: bool
    previous_possession_has_timeout: bool
    previous_possession_ending_event: _Event | None

    @property
    def possession_start_type(self) -> str: ...


def pbpstats_dir(data_dir: Path) -> Path:
    return data_dir / "pbpstats"


def install_overrides(data_dir: Path) -> None:
    """Copy the committed override files into `DATA_DIR/pbpstats/overrides/`."""
    directory = pbpstats_dir(data_dir) / "overrides"
    directory.mkdir(parents=True, exist_ok=True)
    for name in OVERRIDE_FILES:
        write_atomic(directory / name, (OVERRIDES_SOURCE / name).read_bytes())


def materialize(cache: RawCache, game_id: str) -> Path:
    """Copy the game's cached live play-by-play into pbpstats's layout, less its `game`
    actions; return the file written.

    The overrides are installed alongside, so pbpstats never reads a stale set.
    """
    content = cache.get(nba_cdn.SOURCE, nba_cdn.PLAYBYPLAY, game_id)
    if content is None:
        path = cache.path(nba_cdn.SOURCE, nba_cdn.PLAYBYPLAY, game_id)
        raise FileNotFoundError(f"game {game_id} has no cached play-by-play at {path}")
    payload: dict[str, Any] = json.loads(content)
    game: dict[str, Any] = payload["game"]
    actions: list[dict[str, Any]] = [a for a in game["actions"] if a["actionType"] != GAME_ACTION]
    payload = {**payload, "game": {**game, "actions": actions}}
    data_dir = cache.root.parent
    install_overrides(data_dir)
    path = pbpstats_dir(data_dir) / "pbp" / f"live_{game_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(payload).encode())
    return path


def stints_for_game(data_dir: Path, game_id: str) -> list[StintRow]:
    """Two rows per stint, one from each team's view, in game order.

    A stint is a span of elapsed clock within a period with the same ten players on the floor.
    """
    spans = _spans(_events(_possessions(data_dir, game_id)))
    rows: list[StintRow] = []
    for span in spans:
        (team, lineup), (opp, opp_lineup) = span.lineups.items()
        for team_id, key, opp_id, opp_key in (
            (team, lineup, opp, opp_lineup),
            (opp, opp_lineup, team, lineup),
        ):
            rows.append(
                StintRow(
                    game_id=game_id,
                    period=span.period,
                    start_sec=span.start_sec,
                    end_sec=span.end_sec,
                    team_id=team_id,
                    lineup_key=_lineup_key(key),
                    opp_lineup_key=_lineup_key(opp_key),
                    possessions=span.possessions[team_id],
                    pts_for=span.points[team_id],
                    pts_against=span.points[opp_id],
                )
            )
    return rows


def possessions_for_game(data_dir: Path, game_id: str) -> list[PossessionRow]:
    """Every possession pbpstats emits for the game, in order."""
    rows: list[PossessionRow] = []
    for possession in _possessions(data_dir, game_id):
        period = possession.period
        ending = possession.events[-1]
        rows.append(
            PossessionRow(
                game_id=game_id,
                period=period,
                number=possession.number,
                offense_team_id=ending.get_offense_team_id(),
                start_sec=_elapsed(period, _seconds_remaining(possession.start_time)),
                end_sec=_elapsed(period, _seconds_remaining(possession.end_time)),
                start_score_margin=possession.start_score_margin,
                start_type=_start_type(possession),
                has_timeout=possession.possession_has_timeout,
                previous_has_timeout=possession.previous_possession_has_timeout,
                end_type=type(ending).__name__.removeprefix("Live"),
                counted=ending.count_as_possession,
            )
        )
    return rows


@dataclass(slots=True)
class _Span:
    period: int
    # Seconds remaining in the period at either end, as pbpstats keeps time.
    start_remaining: float
    end_remaining: float
    lineups: dict[int, frozenset[int]]
    possessions: defaultdict[int, int] = field(default_factory=lambda: defaultdict(int))
    points: defaultdict[int, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def start_sec(self) -> float:
        return _elapsed(self.period, self.start_remaining)

    @property
    def end_sec(self) -> float:
        return _elapsed(self.period, self.end_remaining)

    def touches(self, period: int, remaining: float) -> bool:
        return period == self.period and self.start_remaining >= remaining >= self.end_remaining


def _possessions(data_dir: Path, game_id: str) -> list[_Possession]:
    source = LivePossessionFileLoader(str(pbpstats_dir(data_dir)))
    try:
        loader = LivePossessionLoader(game_id, source)
    except Exception as exc:
        # pbpstats raises a bare `Exception` for a missing file and its own types for bad data;
        # callers catch `PbpError` so that they need not import pbpstats.
        raise PbpError(f"game {game_id}: {exc}") from exc
    return cast(list[_Possession], loader.items)


def _events(possessions: list[_Possession]) -> list[_Event]:
    return [event for possession in possessions for event in possession.events]


def _spans(events: list[_Event]) -> list[_Span]:
    """The game's stints, with each event's points and possessions credited to one of them."""
    spans: list[_Span] = []
    for event in events:
        previous = event.previous_event
        if previous is None:
            continue
        elapsed = event.seconds_since_previous_event
        if elapsed < 0:
            raise PbpError(f"period {event.period}: the clock runs backwards at {event.clock}")
        if elapsed == 0:
            continue
        lineups = _lineups(previous)
        last = spans[-1] if spans else None
        if last is not None and last.period == event.period and last.lineups == lineups:
            last.end_remaining = event.seconds_remaining
        else:
            spans.append(
                _Span(event.period, previous.seconds_remaining, event.seconds_remaining, lineups)
            )
    for event in events:
        if isinstance(event, (FieldGoal, FreeThrow)) and event.is_made:
            _credit(spans, event).points[event.team_id] += event.shot_value
        if event.count_as_possession:
            _credit(spans, event).possessions[event.get_offense_team_id()] += 1
    return spans


def _lineups(event: _Event) -> dict[int, frozenset[int]]:
    lineups = {team: frozenset(players) for team, players in event.current_players.items()}
    for team, players in lineups.items():
        if len(players) != LINEUP_SIZE:
            raise PbpError(
                f"period {event.period}, {event.clock}: team {team} has {len(players)} players "
                "on the floor; add its period starters to missing_period_starters.json"
            )
    return lineups


def _credit(spans: list[_Span], event: _Event) -> _Span:
    """The stint an event's points or possession belong to (see the module docstring)."""
    # pbpstats credits a free throw to the ten on the floor at the foul.
    source = event.event_for_efficiency_stats if isinstance(event, FreeThrow) else event
    players = {team: frozenset(on_floor) for team, on_floor in source.current_players.items()}
    touching = [s for s in spans if s.touches(event.period, event.seconds_remaining)]
    if not touching:
        raise PbpError(f"period {event.period}: no stint is on the floor at {event.clock}")
    # The latest of the best-matching stints, so a tie goes to the ten that plays on.
    return max(
        reversed(touching),
        key=lambda s: sum(
            len(s.lineups[team] & players.get(team, frozenset())) for team in s.lineups
        ),
    )


def _lineup_key(players: frozenset[int]) -> str:
    return "-".join(str(player) for player in sorted(players))


def _period_seconds(period: int) -> int:
    return REGULATION_PERIOD_SECONDS if period <= REGULATION_PERIODS else OVERTIME_PERIOD_SECONDS


def _elapsed(period: int, remaining: float) -> float:
    return _period_seconds(period) - remaining


def _seconds_remaining(clock: str) -> float:
    """`PT04M35.00S` → 275.0, as pbpstats's live events read their clock."""
    minutes, seconds = clock.removeprefix("PT").removesuffix("S").split("M")
    return int(minutes) * 60 + float(seconds)


def _start_type(possession: _Possession) -> str | None:
    try:
        return possession.possession_start_type
    except AttributeError:
        # pbpstats looks up the shot type of the missed shot behind a rebound; a heave has none.
        missed = getattr(possession.previous_possession_ending_event, "missed_shot", None)
        if getattr(missed, "action_type", None) != HEAVE_ACTION:
            raise
        return None
