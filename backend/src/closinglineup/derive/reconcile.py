"""Reconciliation of our stints against NBA.com's lineup and on-court ratings (spec §7.4, §11, §14).

Nothing else proves the stints right: a play-by-play ordering error or a substitution bug moves
points and possessions between lineups without changing a final score. So each 5-man lineup's
totals, summed over every stint it played, are compared with `leaguedashlineups`, and each
player's on-court totals with `leagueplayerondetails`. The sums are unfiltered, garbage time
included, since NBA.com counts every possession.

Ratings are per 100 possessions: offense over the team's own possessions, defense over the
opponent's, as NBA.com computes them. Possessions are credited as NBA.com credits them, which is
not how a stint counts them (NEU-1899): to every five on the court for any part of one, so one a
substitution splits counts for both, and at a substitution logged at the instant one starts or
ends, as its events there decide (NEU-1900). `parse.pbp` emits those fives as `possession_lineup`
rows, and a player has the possession if any of them holds him. A stint's `possessions` stay
pbpstats' counted ones, credited once, for the dashboard's own ratings (spec §7.3). Points are the
stints', a free throw's at its foul, which is where NBA.com credits them too.

Only the lineups and players NBA.com credits with at least `MIN_POSSESSIONS` (its `POSS`) are
compared, and only on net rating, which NBA.com rounds to 0.1. A player is within ±0.5. A lineup
is within `LINEUP_TOLERANCE_POSSESSIONS` possessions' worth of net rating: the events at a
substitution do not settle every possession there as NBA.com does, and one possession moves a
lineup's rating by about 110 / `POSS`.

`leagueplayerondetails` lists only the On rows, so the on/off check is each player's on-court
net rating.

A row outside tolerance names the games that pull our net rating furthest from NBA.com's: the
games whose removal brings it closest. A game a parser got wrong surfaces there; a game we have no
stints for does not, but shows as a games count short of NBA.com's.

Both NBA.com responses are read from the raw cache and are season-to-date as of their fetch, so
the comparison holds only when the stints are loaded through the same games;
`nba_stats.fetch_reconciliation` refreshes them.
"""

import json
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Literal, Self

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import Possession, PossessionLineup, Stint
from closinglineup.parse.rows import PossessionLineupRow, PossessionRow, StintRow
from closinglineup.sources import nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_stats import SeasonType
from closinglineup.teams import TEAMS, by_nba_team_id

type Payload = dict[str, Any]
type Kind = Literal["lineup", "on_court"]
# A lineup's (team ID, lineup key), or a player's (team ID, player ID as text).
type Key = tuple[int, str]

# How NBA.com counts, measured on 2025-26 (NEU-1899): its team `POSS` is pbpstats' every possession
# (+0.13%), not its counted ones (-0.69%) or the box-score estimate (+1.09%), and its players'
# on-court `POSS` sum to 1.27% more than their teams', a possession split by a substitution
# counting for both sides of it.
MIN_POSSESSIONS = 100
LINEUP_TOLERANCE_POSSESSIONS = 3
# Points per possession, about the league's: what one possession is worth to a rating.
POINTS_PER_POSSESSION = 1.1
ON_COURT_TOLERANCE = 0.5
TOP_GAMES = 3
# Two numbers equal to 0.1 can differ by more than 0.1 in floating point.
EPSILON = 1e-9

# The season-type digit of a game ID (`season`). The NBA Cup championship (6) is in none of them.
SEASON_TYPE_DIGITS = {
    SeasonType.REGULAR_SEASON: "2",
    SeasonType.PLAYOFFS: "4",
    SeasonType.PLAY_IN: "5",
}
ON_COURT = "On"


class ReconcileError(Exception):
    """Reconciliation cannot run on what is cached and loaded."""


class MissingResponseError(ReconcileError, LookupError):
    """A response reconciliation compares against is not in the raw cache."""


@dataclass(frozen=True, slots=True)
class Totals:
    """Possessions and points, either way, and the ratings per 100 possessions they make."""

    possessions: int = 0
    opp_possessions: int = 0
    pts_for: int = 0
    pts_against: int = 0

    def __add__(self, other: Self) -> Self:
        return type(self)(
            self.possessions + other.possessions,
            self.opp_possessions + other.opp_possessions,
            self.pts_for + other.pts_for,
            self.pts_against + other.pts_against,
        )

    def __sub__(self, other: Self) -> Self:
        return type(self)(
            self.possessions - other.possessions,
            self.opp_possessions - other.opp_possessions,
            self.pts_for - other.pts_for,
            self.pts_against - other.pts_against,
        )

    @property
    def off_rating(self) -> float | None:
        return 100 * self.pts_for / self.possessions if self.possessions else None

    @property
    def def_rating(self) -> float | None:
        return 100 * self.pts_against / self.opp_possessions if self.opp_possessions else None

    @property
    def net_rating(self) -> float | None:
        off, defense = self.off_rating, self.def_rating
        return None if off is None or defense is None else off - defense


@dataclass(slots=True)
class Tally:
    """A lineup's or a player's totals, game by game."""

    by_game: dict[str, Totals] = field(default_factory=dict[str, Totals])

    def add(self, game_id: str, totals: Totals) -> None:
        self.by_game[game_id] = self.by_game.get(game_id, Totals()) + totals

    @property
    def total(self) -> Totals:
        return sum(self.by_game.values(), Totals())


@dataclass(frozen=True, slots=True)
class NbaRatings:
    """One `leaguedashlineups` or `leagueplayerondetails` row: `key` is the lineup key, or the
    player ID."""

    team_id: int
    key: str
    name: str
    games: int
    possessions: int
    off_rating: float
    def_rating: float
    net_rating: float


@dataclass(frozen=True, slots=True)
class Check:
    """One lineup's or player's totals beside NBA.com's."""

    kind: Kind
    nba: NbaRatings
    ours: Totals
    games: int
    tolerance: float
    # The games that pull our net rating furthest from NBA.com's, worst first; only outside
    # tolerance.
    top_games: tuple[str, ...] = ()

    @property
    def diff(self) -> float | None:
        """Our net rating less NBA.com's, or None if we have no possessions for it."""
        net = self.ours.net_rating
        return None if net is None else net - self.nba.net_rating

    @property
    def within(self) -> bool:
        diff = self.diff
        return diff is not None and abs(diff) <= self.tolerance + EPSILON


@dataclass(frozen=True, slots=True)
class Report:
    season: str
    season_type: SeasonType
    team_ids: tuple[int, ...]
    lineups: list[Check]
    on_court: list[Check]

    @property
    def outside(self) -> list[Check]:
        """Every check outside tolerance, lineups first."""
        return [check for check in [*self.lineups, *self.on_court] if not check.within]


def tallies(
    stints: Iterable[StintRow],
    possessions: Iterable[PossessionRow],
    possession_lineups: Iterable[PossessionLineupRow],
) -> tuple[dict[Key, Tally], dict[Key, Tally]]:
    """Each lineup's totals and each player's on-court totals, per team: the stints' points, and
    the possessions NBA.com credits to each lineup, and to each player in any of them."""
    lineups: defaultdict[Key, Tally] = defaultdict(Tally)
    players: defaultdict[Key, Tally] = defaultdict(Tally)
    for stint in stints:
        points = Totals(pts_for=stint.pts_for, pts_against=stint.pts_against)
        lineups[(stint.team_id, stint.lineup_key)].add(stint.game_id, points)
        for player in stint.lineup_key.split("-"):
            players[(stint.team_id, player)].add(stint.game_id, points)
    offense = {(p.game_id, p.period, p.number): p.offense_team_id for p in possessions}
    on_court: defaultdict[tuple[str, int, int, int], set[str]] = defaultdict(set)
    for row in possession_lineups:
        on_court[(row.game_id, row.period, row.number, row.team_id)].add(row.lineup_key)
    for (game_id, period, number, team_id), keys in on_court.items():
        on_offense = team_id == offense[(game_id, period, number)]
        credit = Totals(possessions=1) if on_offense else Totals(opp_possessions=1)
        for key in keys:
            lineups[(team_id, key)].add(game_id, credit)
        for player in {player for key in keys for player in key.split("-")}:
            players[(team_id, player)].add(game_id, credit)
    return dict(lineups), dict(players)


def _rows(payload: Payload) -> list[dict[str, Any]]:
    result_set: Payload = payload["resultSets"][0]
    headers: list[str] = result_set["headers"]
    return [dict(zip(headers, row, strict=True)) for row in result_set["rowSet"]]


def _ratings(row: dict[str, Any], key: str, name: str) -> NbaRatings:
    return NbaRatings(
        team_id=int(row["TEAM_ID"]),
        key=key,
        name=name,
        games=int(row["GP"]),
        possessions=int(row["POSS"]),
        off_rating=float(row["OFF_RATING"]),
        def_rating=float(row["DEF_RATING"]),
        net_rating=float(row["NET_RATING"]),
    )


def nba_lineups(payload: Payload) -> list[NbaRatings]:
    """`leaguedashlineups`'s rows, each `GROUP_ID` (`-203497-...-1630183-`) as a lineup key."""
    return [
        _ratings(row, _lineup_key(row["GROUP_ID"]), row["GROUP_NAME"]) for row in _rows(payload)
    ]


def nba_on_court(payload: Payload) -> list[NbaRatings]:
    """`leagueplayerondetails`'s On rows, keyed by player ID."""
    return [
        _ratings(row, str(row["VS_PLAYER_ID"]), row["VS_PLAYER_NAME"])
        for row in _rows(payload)
        if row["COURT_STATUS"] == ON_COURT
    ]


def _lineup_key(group_id: str) -> str:
    """As `parse.pbp` keys a lineup: player IDs sorted as numbers, joined by `-`."""
    return "-".join(str(p) for p in sorted(int(p) for p in group_id.split("-") if p))


def lineup_tolerance(nba: NbaRatings) -> float:
    """`LINEUP_TOLERANCE_POSSESSIONS` possessions' worth of net rating, at NBA.com's possessions."""
    return LINEUP_TOLERANCE_POSSESSIONS * POINTS_PER_POSSESSION * 100 / nba.possessions


def on_court_tolerance(nba: NbaRatings) -> float:
    return ON_COURT_TOLERANCE


def compare(
    kind: Kind,
    ours: dict[Key, Tally],
    theirs: Iterable[NbaRatings],
    *,
    tolerance: Callable[[NbaRatings], float],
    min_possessions: int = MIN_POSSESSIONS,
) -> list[Check]:
    """A check per NBA.com row with at least `min_possessions`, most possessions first."""
    checks: list[Check] = []
    for nba in sorted(theirs, key=lambda n: (-n.possessions, n.team_id, n.key)):
        if nba.possessions < min_possessions:
            continue
        tally = ours.get((nba.team_id, nba.key), Tally())
        check = Check(kind, nba, tally.total, len(tally.by_game), tolerance(nba))
        if not check.within:
            check = replace(check, top_games=top_games(tally, nba.net_rating))
        checks.append(check)
    return checks


def top_games(tally: Tally, target: float, limit: int = TOP_GAMES) -> tuple[str, ...]:
    """The games whose removal brings the tally's net rating closest to `target`, best first,
    leaving out any whose removal does not bring it closer at all."""
    total = tally.total
    net = total.net_rating
    if net is None:
        return ()
    gap = abs(net - target)
    scored: list[tuple[float, str]] = []
    for game_id, totals in tally.by_game.items():
        rest = (total - totals).net_rating
        # A tally's only game takes the whole gap with it.
        closer = gap - (0.0 if rest is None else abs(rest - target))
        if closer > EPSILON:
            scored.append((closer, game_id))
    return tuple(game_id for _, game_id in sorted(scored, key=lambda s: (-s[0], s[1]))[:limit])


def season_rows(
    db: sessionmaker[Session], season: str, season_type: SeasonType, team_ids: Iterable[int]
) -> tuple[list[StintRow], list[PossessionRow], list[PossessionLineupRow]]:
    """Every stint, possession and possession lineup of the games `team_ids` played in the
    season type, both teams'."""
    # `0022500001`: league `00`, season type, then the season's two-digit start year.
    prefix = f"00{SEASON_TYPE_DIGITS[season_type]}{season[2:4]}"
    games = (
        select(Stint.game_id)
        .where(Stint.game_id.startswith(prefix), Stint.team_id.in_(list(team_ids)))
        .distinct()
    )
    with db() as session:
        stints = [
            StintRow(**row)
            for row in session.execute(
                select(*_columns(Stint, StintRow)).where(Stint.game_id.in_(games))
            ).mappings()
        ]
        possessions = [
            PossessionRow(**row)
            for row in session.execute(
                select(*_columns(Possession, PossessionRow)).where(Possession.game_id.in_(games))
            ).mappings()
        ]
        possession_lineups = [
            PossessionLineupRow(**row)
            for row in session.execute(
                select(*_columns(PossessionLineup, PossessionLineupRow)).where(
                    PossessionLineup.game_id.in_(games)
                )
            ).mappings()
        ]
    return stints, possessions, possession_lineups


def _columns(
    model: type[Stint | Possession | PossessionLineup],
    row: type[StintRow | PossessionRow | PossessionLineupRow],
) -> list[Any]:
    return [model.__table__.c[name] for name in row.model_fields]


def _cached(cache: RawCache, endpoint: str, key: str) -> Payload:
    raw = cache.get(nba_stats.SOURCE, endpoint, key)
    if raw is None:
        raise MissingResponseError(f"{endpoint} {key} is not in the raw cache")
    return json.loads(raw)


def reconcile(
    db: sessionmaker[Session],
    cache: RawCache,
    season: str,
    season_type: SeasonType,
    team_ids: Iterable[int],
    *,
    min_possessions: int = MIN_POSSESSIONS,
) -> Report:
    """`team_ids`'s lineups and on-court ratings for the season type, beside NBA.com's cached
    ones.

    Raises `MissingResponseError` if `leaguedashlineups` or a team's `leagueplayerondetails` is
    not cached.
    """
    teams = tuple(team_ids)
    lineups_key = nba_stats.league_dash_lineups_key(season, season_type)
    league_lineups = nba_lineups(_cached(cache, nba_stats.LEAGUE_DASH_LINEUPS, lineups_key))
    on_court: list[NbaRatings] = []
    for team_id in teams:
        key = nba_stats.league_player_on_details_key(team_id, season, season_type)
        on_court.extend(nba_on_court(_cached(cache, nba_stats.LEAGUE_PLAYER_ON_DETAILS, key)))
    lineup_totals, on_court_totals = tallies(*season_rows(db, season, season_type, teams))
    return Report(
        season=season,
        season_type=season_type,
        team_ids=teams,
        lineups=compare(
            "lineup",
            lineup_totals,
            [n for n in league_lineups if n.team_id in teams],
            tolerance=lineup_tolerance,
            min_possessions=min_possessions,
        ),
        on_court=compare(
            "on_court",
            on_court_totals,
            on_court,
            tolerance=on_court_tolerance,
            min_possessions=min_possessions,
        ),
    )


def sample_teams(day: date, size: int = 2) -> tuple[int, ...]:
    """The teams the nightly job reconciles for `day`: `size` consecutive teams of `TEAMS`,
    moving on by `size` each day, so every team comes round every 30 / `size` days."""
    start = day.toordinal() * size
    return tuple(TEAMS[(start + n) % len(TEAMS)].nba_team_id for n in range(size))


def season_type_of(game_id: str) -> SeasonType | None:
    """The season type a game's lineups are counted in, or None for one in none of them."""
    digit = game_id[2:3]
    return next((t for t, d in SEASON_TYPE_DIGITS.items() if d == digit), None)


def _rating(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def describe(check: Check) -> str:
    """One line for a check: what it is, our numbers beside NBA.com's, and its top games."""
    tricode = by_nba_team_id(check.nba.team_id).tricode
    subject = f"{tricode} {check.kind} {check.nba.key} ({check.nba.name})"
    diff = check.diff
    delta = "n/a" if diff is None else f"{diff:+.2f}"
    ours, nba = check.ours, check.nba
    line = (
        f"{subject}: net {_rating(ours.net_rating)} vs {nba.net_rating:.1f} "
        f"({delta}, tolerance ±{check.tolerance:.2f}), "
        f"ortg {_rating(ours.off_rating)} vs {nba.off_rating:.1f}, "
        f"drtg {_rating(ours.def_rating)} vs {nba.def_rating:.1f}, "
        f"poss {ours.possessions} vs {nba.possessions}, games {check.games} vs {nba.games}"
    )
    if check.top_games:
        line += f"; top games {', '.join(check.top_games)}"
    return line
