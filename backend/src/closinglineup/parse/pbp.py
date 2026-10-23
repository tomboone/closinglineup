"""The pbpstats bridge: cached play-by-play → `StintRow`s, `PossessionRow`s and
`PossessionLineupRow`s (spec §3.1, §6.2).

pbpstats fetches with `requests`, which NBA.com blocks, so it only ever runs in file mode over a
directory this module fills from the raw cache (ADR-0002). This is the only importer of pbpstats.

The play-by-play is cdn.nba.com's live feed, which pbpstats reads through its `live` loaders.
stats.nba.com's `playbyplayv2`, which its `stats_nba` loaders read, answers `{}` for every game
(NEU-1855), so those loaders and `boxscoretraditionalv2` are not used.

File layout, as pbpstats 1.3.11 (`PBPSTATS_VERSION`) reads it under `DATA_DIR/pbpstats/`:

    pbp/live_<game_id>.json       `raw/cdn/playbyplay/<game_id>.json`, copied by `materialize`
                                  less its `game` actions, with the 2026-27 fixes (below) and
                                  the game's action overrides
    overrides/missing_period_starters.json
                                  {game_id: {period: {team_id: [five player IDs]}}}, for a period
                                  whose starters pbpstats cannot infer from its events

The live loaders read nothing else: no box score, and no other override. They do load
`possession_change_event_overrides.json` and `non_possession_changing_event_overrides.json`, but
only the `stats_nba` events consult them, so they are not committed; nor is
`bad_pbp_possessions.json`, which only the `stats_nba` possession loader reads. Their one override
file is committed under `backend/pbpstats_overrides/`; `materialize` copies it in beside each game
it writes, so pbpstats always reads the committed version.

pbpstats has no override for an action's own fields, so this module keeps one beside it,
`backend/pbpstats_overrides/action_overrides.json`: {game_id: {action_number: {field: value}}},
for a scorer's error in the feed, such as an action stamped later on the clock than the one after
it. `materialize` applies it to the actions it writes; pbpstats never reads it. Each entry in
either file has its reason in the README beside them.

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

And in 2026-27 feeds, which pbpstats 1.3.11 cannot read as they come (NEU-1897):

- An action with no player (period start and end, timeouts, team rebounds and turnovers) has no
  `personId`, where 2025-26 feeds sent 0. pbpstats reads it into `player1_id` and fails at the
  first period start, so `materialize` gives such actions `personId` 0.
- `actions` is not always in `orderNumber` order: a rebound can be listed just before the shot
  it names. pbpstats finds a rebound's shot by walking back through the events before it, and
  fails when it is not there, so `materialize` sorts the actions by `orderNumber`.

Possession lineups are the fives NBA.com credits with each possession, which reconciliation sums
(NEU-1900). A stint credits each possession once; NBA.com credits it to every five on the floor
for any part of it, and where a substitution is logged at the instant a possession starts or ends,
it decides by the events there, not the clock. Matched on NBA.com's per-game `leaguedashlineups`
and `leagueplayerondetails` for 32 games of 2025-26, a possession counts for each team's fives:

- on the floor while the clock runs in it;
- on the floor at the events that open it at its starting instant, up to the team's first
  substitution or a timeout there, and at the defensive rebound that won it, if the team's
  substitution there comes before any timeout;
- on the floor at any rebound in it, a missed free throw's included;
- on the floor at its ending event.

Each event is taken where pbpstats credits its points: a free throw at its foul, which is how
NBA.com credits free-throw points too (32 games, 1,057 fives: at the foul, every five's offensive
rating matches; at the shot, over a hundred do not). A possession with no time that ends a period
uncounted, or holds nothing but substitutions, counts for no one. One with no time and no five
at any of those events goes to the five on the floor just before it.
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
    EndOfPeriod,
    FieldGoal,
    FreeThrow,
    Rebound,
    Substitution,
    Timeout,
)

from closinglineup.parse.rows import PossessionLineupRow, PossessionRow, StintRow
from closinglineup.sources import nba_cdn
from closinglineup.sources.cache import RawCache, write_atomic

PBPSTATS_VERSION = "1.3.11"

# `backend/pbpstats_overrides/`: `parents[3]` of `backend/src/closinglineup/parse/pbp.py`, and of
# `/app/src/closinglineup/parse/pbp.py` in the image.
OVERRIDES_SOURCE = Path(__file__).resolve().parents[3] / "pbpstats_overrides"
OVERRIDE_FILES = ("missing_period_starters.json",)
# Read by `materialize`, not pbpstats, so it is not installed with them.
ACTION_OVERRIDES = "action_overrides.json"

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
    actions, each with a `personId`, its action overrides applied, and in `orderNumber` order;
    return the file written.

    The overrides are installed alongside, so pbpstats never reads a stale set.
    """
    content = cache.get(nba_cdn.SOURCE, nba_cdn.PLAYBYPLAY, game_id)
    if content is None:
        path = cache.path(nba_cdn.SOURCE, nba_cdn.PLAYBYPLAY, game_id)
        raise FileNotFoundError(f"game {game_id} has no cached play-by-play at {path}")
    payload: dict[str, Any] = json.loads(content)
    game: dict[str, Any] = payload["game"]
    actions: list[dict[str, Any]] = sorted(
        _override_actions(
            game_id,
            [{"personId": 0, **a} for a in game["actions"] if a["actionType"] != GAME_ACTION],
        ),
        key=lambda a: a["orderNumber"],
    )
    payload = {**payload, "game": {**game, "actions": actions}}
    data_dir = cache.root.parent
    install_overrides(data_dir)
    path = pbpstats_dir(data_dir) / "pbp" / f"live_{game_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(payload).encode())
    return path


def _override_actions(game_id: str, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`actions` with the game's committed action overrides applied."""
    committed: dict[str, dict[str, dict[str, Any]]] = json.loads(
        (OVERRIDES_SOURCE / ACTION_OVERRIDES).read_text()
    )
    overrides = {int(number): fields for number, fields in committed.get(game_id, {}).items()}
    missing = overrides.keys() - {a["actionNumber"] for a in actions}
    if missing:
        raise PbpError(
            f"game {game_id}: {ACTION_OVERRIDES} names actions not in the feed: {sorted(missing)}"
        )
    return [{**a, **overrides.get(a["actionNumber"], {})} for a in actions]


@dataclass(frozen=True, slots=True)
class GameRows:
    stints: list[StintRow]
    possessions: list[PossessionRow]
    possession_lineups: list[PossessionLineupRow]


def rows_for_game(data_dir: Path, game_id: str) -> GameRows:
    """The game's stints, possessions and possession lineups, from one pbpstats read."""
    possessions = _possessions(data_dir, game_id)
    spans = _spans(_events(possessions))
    return GameRows(
        _stint_rows(game_id, spans),
        _possession_rows(game_id, possessions),
        _possession_lineup_rows(game_id, possessions, spans),
    )


def stints_for_game(data_dir: Path, game_id: str) -> list[StintRow]:
    """Two rows per stint, one from each team's view, in game order.

    A stint is a span of elapsed clock within a period with the same ten players on the floor.
    """
    return _stint_rows(game_id, _spans(_events(_possessions(data_dir, game_id))))


def possessions_for_game(data_dir: Path, game_id: str) -> list[PossessionRow]:
    """Every possession pbpstats emits for the game, in order."""
    return _possession_rows(game_id, _possessions(data_dir, game_id))


def _stint_rows(game_id: str, spans: list["_Span"]) -> list[StintRow]:
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


def _possession_rows(game_id: str, possessions: list[_Possession]) -> list[PossessionRow]:
    rows: list[PossessionRow] = []
    for possession in possessions:
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


def _possession_lineup_rows(
    game_id: str, possessions: list[_Possession], spans: list["_Span"]
) -> list[PossessionLineupRow]:
    """Each five NBA.com credits with each possession (see the module docstring)."""
    rows: list[PossessionLineupRow] = []
    previous: _Possession | None = None
    for possession in possessions:
        if previous is not None and previous.period != possession.period:
            previous = None
        for team_id, fives in _credited(previous, possession, spans).items():
            rows.extend(
                PossessionLineupRow(
                    game_id=game_id,
                    period=possession.period,
                    number=possession.number,
                    team_id=team_id,
                    lineup_key=key,
                )
                for key in sorted(_lineup_key(five) for five in fives)
            )
        previous = possession
    return rows


def _credited(
    previous: _Possession | None, possession: _Possession, spans: list["_Span"]
) -> dict[int, set[frozenset[int]]]:
    """Each team's fives NBA.com credits with `possession`, which follows `previous` in its
    period."""
    period = possession.period
    start = _seconds_remaining(possession.start_time)
    end = _seconds_remaining(possession.end_time)
    ending = possession.events[-1]
    instant = start == end
    if instant and (
        (isinstance(ending, EndOfPeriod) and not ending.count_as_possession)
        or isinstance(ending, Substitution)
    ):
        return {}
    in_period = [s for s in spans if s.period == period]
    credited: dict[int, set[frozenset[int]]] = {}
    for team_id in spans[0].lineups:
        fives: set[frozenset[int]] = set()
        if not instant:
            fives.update(
                s.lineups[team_id]
                for s in in_period
                if s.start_remaining > end and s.end_remaining < start
            )
        events = [] if instant else _opening(previous, possession, team_id, start)
        events += [e for e in possession.events if isinstance(e, Rebound)]
        events.append(ending)
        for event in events:
            on_floor = _efficiency_event(event).current_players.get(team_id, [])
            if len(on_floor) == LINEUP_SIZE:
                fives.add(frozenset(on_floor))
        if not fives and in_period:
            # The five on the floor just before it: the latest stint to start before it ends, or
            # the period's first.
            before = [s for s in in_period if s.start_remaining > end]
            nearest = (
                min(before, key=lambda s: s.start_remaining)
                if before
                else max(in_period, key=lambda s: s.start_remaining)
            )
            fives.add(nearest.lineups[team_id])
        credited[team_id] = fives
    return credited


def _opening(
    previous: _Possession | None, possession: _Possession, team_id: int, start: float
) -> list[_Event]:
    """The events that open `possession` at its starting instant, up to the team's first
    substitution or a timeout there; and the defensive rebound that won it, if the team's
    substitution comes before any timeout."""
    events: list[_Event] = []
    for event in possession.events:
        if event.seconds_remaining != start or isinstance(event, Timeout):
            return events
        if isinstance(event, Substitution):
            if event.team_id != team_id:
                continue
            won = previous.events[-1] if previous is not None else None
            if isinstance(won, Rebound) and won.seconds_remaining == start:
                events.append(won)
            return events
        events.append(event)
    return events


def _efficiency_event(event: _Event) -> _Event:
    """Where pbpstats credits an event's points: a free throw at its foul."""
    return event.event_for_efficiency_stats if isinstance(event, FreeThrow) else event


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
    source = _efficiency_event(event)
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
