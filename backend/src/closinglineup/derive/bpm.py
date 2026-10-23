"""Box Plus/Minus 2.0, computed here from `player_game` and written to `impact_snapshot` as
`source='bpm_local'`: the impact metric that is always on (spec §4.5, §6.3).

Formula: Daniel Myers, "About Box Plus/Minus (BPM)", Basketball Reference, 2020,
https://www.basketball-reference.com/about/bpm2.html, and the calculator sheet it links to for
what the page leaves implicit: the 1.00 points per true shot attempt the regression was fit at,
that a player's share of his team is his minutes over the team's, so a team's shares sum to five,
and that the team adjustment is a fifth of the gap. The sheet's worked example, the 2016-17
Cavaliers, is reproduced in `tests/test_bpm.py`.

The calculation, per team over the regular-season games final through the snapshot's date:

1. Every stat is per 100 team possessions with the player on the floor: his share of the team's
   minutes times its possessions, estimated from the box score as Basketball Reference does
   (`possessions`).
2. His position (1.0, a point guard, to 5.0, a center) and offensive role (1.0, a creator, to 5.0,
   a receiver) are regressed from his shares of the team's rebounds, steals, fouls, assists and
   blocks, and of its assists and "threshold points", the points above a shooting efficiency 0.33
   below the team's. Each is blended with 50 minutes of a prior (the position he is listed at; a
   role of 4.0), clamped to [1, 5], and shifted so the team's minutes-weighted average is 3.0.
3. Each coefficient is read at his position, linearly between its values at 1.0 and 5.0, the two
   shot-attempt ones at his role; points are first adjusted for the team's shooting, so a scorer
   is measured against how his team's shots go in rather than the league's. The sum, plus a
   position constant and a role constant, is his raw BPM.
4. The team's raw BPMs, weighted by minutes share, sum to its net rating per 100 possessions,
   corrected for the lead it played with: the constant that makes them is added to every player.
   OBPM goes the same way against the team's offensive rating relative to the league's; DBPM is
   the difference.

Where this departs from the published values: the team rating is the team's own net rating, not
one adjusted for its schedule, and its average lead is taken as half its margin per game; the
listed position is NBA.com's coarse one (`G`, `F-C`, ...) rather than one of five; and turnovers
charged to the team rather than a player are not in the rows summed. Each is spread over five
players by the team adjustment or is a prior worth 50 minutes, so a season's values land within a
point of the published ones (`tests/test_bpm.py`).

A player with a minute or more has a row; one who played for more than one team has one row, his
teams' values weighted by his minutes on each. `nightly` writes the snapshot for its date after
loading it; a re-run computes the same rows, and `replace_snapshot` writes nothing.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import Game, Player, PlayerGame
from closinglineup.impact import ImpactSnapshotRow, ImpactSource, replace_snapshot
from closinglineup.season import season_label

SOURCE: ImpactSource = "bpm_local"
# A player has a row once he has played this many minutes.
MIN_MINUTES = 1.0
# Regular-season game IDs: `00`, then season type 2 (`season`). The Cup final, type 6, is left out,
# as it is from the league's regular-season totals.
REGULAR_SEASON_PREFIX = "002"


class Weights(NamedTuple):
    """One stat's coefficient at position (or offensive role) 1.0 and 5.0, in the BPM regression
    and in the OBPM one; between, it varies linearly."""

    bpm_1: float
    bpm_5: float
    obpm_1: float
    obpm_5: float


# The regressions' coefficients, on stats per 100 possessions; `fga` and `fta` run along the
# player's offensive role, every other along his position (BRef, "The Base Regression" and
# "Offensive Box Plus/Minus Regression").
COEFFICIENTS: dict[str, Weights] = {
    "pts": Weights(0.860, 0.860, 0.605, 0.605),
    "fg3m": Weights(0.389, 0.389, 0.477, 0.477),
    "ast": Weights(0.580, 1.034, 0.476, 0.476),
    "tov": Weights(-0.964, -0.964, -0.579, -0.882),
    "orb": Weights(0.613, 0.181, 0.606, 0.422),
    "drb": Weights(0.116, 0.181, -0.112, 0.103),
    "stl": Weights(1.369, 1.008, 0.177, 0.294),
    "blk": Weights(1.327, 0.703, 0.725, 0.097),
    "pf": Weights(-0.367, -0.367, -0.439, -0.439),
    "fga": Weights(-0.560, -0.780, -0.330, -0.472),
    "fta": Weights(-0.246, -0.343, -0.145, -0.208),
}
ROLE_STATS = frozenset({"fga", "fta"})
# The position constant at position 1.0, rising linearly to 0 at 3.0 and above, and the role
# constant at role 5.0, 0 at 3.0 and its negative at 1.0: each for BPM, then for OBPM.
POSITION_CONSTANT = (-0.818, -1.698)
ROLE_CONSTANT = (2.774, 0.860)

# Position from the player's shares of the team's TRB, STL, PF, AST and BLK, after the intercept;
# offensive role from his shares of its AST and threshold points (BRef, appendix).
POSITION_REGRESSION = (2.130, 8.668, -2.486, 0.992, -3.536, 1.667)
ROLE_REGRESSION = (6.000, -6.642, -8.544)
# Each estimate is blended with this many minutes of its prior: the listed position, or a role
# of 4.0.
PRIOR_MINUTES = 50.0
ROLE_PRIOR = 4.0
# Threshold points are those above a shooting efficiency this far below the team's, in points
# per true shot attempt.
THRESHOLD_BELOW_TEAM = 0.33
# Points per true shot attempt the regression was fit at (the sheet's "Baseline Pts/TSA").
BASELINE_PTS_PER_TSA = 1.00
# A true shot attempt is a field goal attempt or this much of a free throw attempt.
FTA_WEIGHT = 0.44
# Points per 100 possessions a team plays worse for each point of lead (Engelmann, cited by BRef);
# half is the team's doing and half its opponent's.
LEAD_EFFECT = 0.35

# A season's regression and constants stay in one list, so the sum over them is one loop.
STATS = tuple(COEFFICIENTS)

# NBA.com's listed positions on the five-point scale; `G` is between point and shooting guard.
LISTED_POSITIONS: dict[str, float] = {
    "G": 1.5,
    "G-F": 2.5,
    "F-G": 2.5,
    "F": 3.5,
    "F-C": 4.5,
    "C-F": 4.5,
    "C": 5.0,
}
UNKNOWN_POSITION = 3.0


@dataclass(frozen=True, slots=True)
class PlayerLine:
    """A player's box-score sums on one team, and the position he is listed at, 1.0 (point guard)
    to 5.0 (center); 3.0 when unknown."""

    player_id: int
    team_id: int
    minutes: float
    listed_position: float
    pts: int
    fga: int
    fg3m: int
    fta: int
    orb: int
    drb: int
    ast: int
    stl: int
    blk: int
    tov: int
    pf: int


class TeamBox(NamedTuple):
    """The box-score totals the possession estimate reads: a team's, or its opponents'."""

    fgm: int
    fga: int
    fta: int
    orb: int
    drb: int
    tov: int


@dataclass(frozen=True, slots=True)
class TeamLine:
    """A team's season: its games, minutes (48 per regulation game, plus overtime), possessions,
    and points for and against."""

    team_id: int
    games: int
    minutes: float
    possessions: float
    pts: int
    opp_pts: int


@dataclass(frozen=True, slots=True)
class PlayerBpm:
    """A player's BPM on one team, with the position and offensive role it was computed at."""

    player_id: int
    team_id: int
    minutes: float
    position: float
    role: float
    bpm: float
    obpm: float

    @property
    def dbpm(self) -> float:
        return self.bpm - self.obpm


def possessions(team: TeamBox, opp: TeamBox) -> float:
    """Basketball Reference's estimate of the possessions in a team's games, from its box-score
    totals and its opponents': the mean of the estimate from each side."""

    def one(a: TeamBox, b: TeamBox) -> float:
        boards = a.orb + b.drb
        putbacks = 1.07 * (a.orb / boards if boards else 0.0) * (a.fga - a.fgm)
        return a.fga + 0.4 * a.fta - putbacks + a.tov

    return 0.5 * (one(team, opp) + one(opp, team))


def listed_position(position: str | None) -> float:
    """NBA.com's listed position on the five-point scale; 3.0 for none or one not known."""
    return LISTED_POSITIONS.get(position or "", UNKNOWN_POSITION)


def _clamp(value: float) -> float:
    return min(max(value, 1.0), 5.0)


def _between(low: float, high: float, at: float) -> float:
    """`low` at 1.0, `high` at 5.0, and linear between."""
    return low + (high - low) * (at - 1.0) / 4.0


def _with_prior(estimate: float, minutes: float, prior: float) -> float:
    return (estimate * minutes + prior * PRIOR_MINUTES) / (minutes + PRIOR_MINUTES)


def shift_to_mean(
    values: Sequence[float], weights: Sequence[float], target: float = 3.0
) -> list[float]:
    """`values` clamped to [1, 5] after the one constant is added to all of them that makes their
    weighted mean `target`; just clamped when the weights are all zero.

    The clamped mean is a nondecreasing, piecewise-linear function of the constant, so the
    constant is found by bisection.
    """
    total = sum(weights)
    if total <= 0:
        return [_clamp(value) for value in values]

    def mean(shift: float) -> float:
        return sum(_clamp(v + shift) * w for v, w in zip(values, weights, strict=True)) / total

    low, high = 1.0 - max(values), 5.0 - min(values)
    for _ in range(60):
        mid = (low + high) / 2
        if mean(mid) < target:
            low = mid
        else:
            high = mid
    shift = (low + high) / 2
    return [_clamp(value + shift) for value in values]


def _box(line: PlayerLine) -> dict[str, float]:
    return {
        "pts": line.pts,
        "fga": line.fga,
        "fg3m": line.fg3m,
        "fta": line.fta,
        "orb": line.orb,
        "drb": line.drb,
        "ast": line.ast,
        "stl": line.stl,
        "blk": line.blk,
        "tov": line.tov,
        "pf": line.pf,
    }


def _raw(per100: Mapping[str, float], position: float, role: float, *, offensive: bool) -> float:
    """The regression's sum at the player's position and role, constants included."""
    total = 0.0
    for stat, weights in COEFFICIENTS.items():
        low, high = (
            (weights.obpm_1, weights.obpm_5) if offensive else (weights.bpm_1, weights.bpm_5)
        )
        total += _between(low, high, role if stat in ROLE_STATS else position) * per100[stat]
    which = 1 if offensive else 0
    total += max(3.0 - position, 0.0) * POSITION_CONSTANT[which] / 2
    total += (role - 3.0) * ROLE_CONSTANT[which] / 2
    return total


def _team_bpm(lines: Sequence[PlayerLine], team: TeamLine, league_ortg: float) -> list[PlayerBpm]:
    played = [line for line in lines if line.minutes > 0]
    if not played or team.minutes <= 0 or team.possessions <= 0:
        return []
    boxes = [_box(line) for line in played]
    totals = {stat: sum(box[stat] for box in boxes) for stat in STATS}
    team_tsa = totals["fga"] + FTA_WEIGHT * totals["fta"]
    if team_tsa <= 0:
        return []
    pts_per_tsa = totals["pts"] / team_tsa
    team_trb = totals["orb"] + totals["drb"]
    team_threshold = THRESHOLD_BELOW_TEAM * team_tsa

    def share_of(value: float, team_value: float, share: float) -> float:
        return value / (team_value * share) if team_value else 0.0

    shares: list[float] = []
    positions: list[float] = []
    roles: list[float] = []
    per100s: list[dict[str, float]] = []
    for line, box in zip(played, boxes, strict=True):
        share = line.minutes / team.minutes
        tsa = box["fga"] + FTA_WEIGHT * box["fta"]
        threshold = box["pts"] - (pts_per_tsa - THRESHOLD_BELOW_TEAM) * tsa
        pct = {stat: share_of(box[stat], totals[stat], share) for stat in STATS}
        pct_trb = share_of(box["orb"] + box["drb"], team_trb, share)
        pct_threshold = share_of(threshold, team_threshold, share)
        p0, p_trb, p_stl, p_pf, p_ast, p_blk = POSITION_REGRESSION
        position = (
            p0
            + p_trb * pct_trb
            + p_stl * pct["stl"]
            + p_pf * pct["pf"]
            + p_ast * pct["ast"]
            + p_blk * pct["blk"]
        )
        r0, r_ast, r_threshold = ROLE_REGRESSION
        role = r0 + r_ast * pct["ast"] + r_threshold * pct_threshold
        on_floor = team.possessions * share
        per100 = {stat: box[stat] * 100 / on_floor for stat in STATS}
        adjusted_pts = box["pts"] - (pts_per_tsa - BASELINE_PTS_PER_TSA) * tsa
        per100["pts"] = adjusted_pts * 100 / on_floor
        shares.append(share)
        positions.append(_with_prior(position, line.minutes, line.listed_position))
        roles.append(_with_prior(role, line.minutes, ROLE_PRIOR))
        per100s.append(per100)
    minutes = [line.minutes for line in played]
    positions = shift_to_mean(positions, minutes)
    roles = shift_to_mean(roles, minutes)
    raw = [
        (
            _raw(per100, position, role, offensive=False),
            _raw(per100, position, role, offensive=True),
        )
        for per100, position, role in zip(per100s, positions, roles, strict=True)
    ]
    margin = team.pts - team.opp_pts
    # The team's average lead, taken as half its final margin; half the effect is its own.
    lead_bonus = LEAD_EFFECT / 2 * (margin / team.games / 2)
    net_rating = margin / team.possessions * 100 + lead_bonus
    off_rating = team.pts / team.possessions * 100 - league_ortg + lead_bonus / 2
    # Shares sum to five, so a fifth of the gap added to each player closes it.
    adjustment = (net_rating - sum(b * s for (b, _), s in zip(raw, shares, strict=True))) / 5
    off_adjustment = (off_rating - sum(o * s for (_, o), s in zip(raw, shares, strict=True))) / 5
    return [
        PlayerBpm(
            line.player_id,
            team.team_id,
            line.minutes,
            position,
            role,
            bpm + adjustment,
            obpm + off_adjustment,
        )
        for line, position, role, (bpm, obpm) in zip(played, positions, roles, raw, strict=True)
    ]


def compute(lines: Iterable[PlayerLine], teams: Mapping[int, TeamLine]) -> list[PlayerBpm]:
    """Every player's BPM on each team, over the lines of `teams`; a line of a team not in
    `teams` has none. The league's offensive rating, which OBPM is relative to, is the teams'
    together."""
    by_team: dict[int, list[PlayerLine]] = defaultdict(list)
    for line in lines:
        by_team[line.team_id].append(line)
    rated = [team for team in teams.values() if team.possessions > 0]
    league_pts = sum(team.pts for team in rated)
    league_possessions = sum(team.possessions for team in rated)
    league_ortg = league_pts / league_possessions * 100 if league_possessions else 0.0
    return [
        result
        for team_id, team_lines in sorted(by_team.items())
        if team_id in teams
        for result in _team_bpm(team_lines, teams[team_id], league_ortg)
    ]


def snapshot_rows(day: date, results: Iterable[PlayerBpm]) -> list[ImpactSnapshotRow]:
    """`day`'s `bpm_local` rows: one per player with `MIN_MINUTES` or more, his teams' values
    weighted by his minutes on each, rounded so a re-run that sums in another order writes
    nothing."""
    by_player: dict[int, list[PlayerBpm]] = defaultdict(list)
    for result in results:
        by_player[result.player_id].append(result)
    rows: list[ImpactSnapshotRow] = []
    for player_id, stints in sorted(by_player.items()):
        minutes = sum(stint.minutes for stint in stints)
        if minutes < MIN_MINUTES:
            continue
        bpm = sum(stint.bpm * stint.minutes for stint in stints) / minutes
        obpm = sum(stint.obpm * stint.minutes for stint in stints) / minutes
        rows.append(
            ImpactSnapshotRow(
                day,
                player_id,
                SOURCE,
                round(bpm, 3),
                round(obpm, 3),
                round(bpm - obpm, 3),
                round(minutes, 1),
            )
        )
    return rows


@dataclass
class _Sums:
    seconds: int = 0
    pts: int = 0
    fgm: int = 0
    fga: int = 0
    fg3m: int = 0
    fta: int = 0
    orb: int = 0
    drb: int = 0
    ast: int = 0
    stl: int = 0
    blk: int = 0
    tov: int = 0
    pf: int = 0
    # The positions the player is listed at, when the sums are one player's.
    position: str | None = None

    def add(self, row: PlayerGame) -> None:
        self.seconds += row.seconds
        self.pts += row.pts
        self.fgm += row.fgm
        self.fga += row.fga
        self.fg3m += row.fg3m
        self.fta += row.fta
        self.orb += row.oreb
        self.drb += row.dreb
        self.ast += row.ast
        self.stl += row.stl
        self.blk += row.blk
        self.tov += row.tov
        self.pf += row.pf

    @property
    def box(self) -> TeamBox:
        return TeamBox(self.fgm, self.fga, self.fta, self.orb, self.drb, self.tov)


@dataclass
class _TeamSums:
    games: int = 0
    minutes: float = 0.0
    possessions: float = 0.0
    pts: int = 0
    opp_pts: int = 0
    players: dict[int, _Sums] = field(default_factory=dict[int, _Sums])


def season_lines(
    session: Session, season: str, through: date
) -> tuple[list[PlayerLine], dict[int, TeamLine]]:
    """Every player's box-score sums on each team, and each team's line, over `season`'s
    regular-season games final through `through`. A game with rows for only one of its teams is
    left out: its possessions cannot be estimated, and the team's minutes would not cover its
    players'."""
    rows = session.execute(
        select(PlayerGame, Player.position)
        .join(Game, Game.game_id == PlayerGame.game_id)
        .outerjoin(Player, Player.nba_player_id == PlayerGame.player_id)
        .where(
            Game.season == season,
            Game.game_date <= through,
            Game.status == "final",
            Game.game_id.like(f"{REGULAR_SEASON_PREFIX}%"),
        )
        .order_by(PlayerGame.game_id, PlayerGame.team_id, PlayerGame.player_id)
    ).all()
    # Each game's rows, by team; a team's box is the sum of its players'.
    games: dict[str, dict[int, list[tuple[PlayerGame, str | None]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row, position in rows:
        games[row.game_id][row.team_id].append((row, position))
    teams: dict[int, _TeamSums] = defaultdict(_TeamSums)
    for sides in games.values():
        if len(sides) != 2:
            continue
        boxes: dict[int, _Sums] = {}
        for team_id, team_rows in sides.items():
            box = _Sums()
            for row, _ in team_rows:
                box.add(row)
            boxes[team_id] = box
        for team_id, team_rows in sides.items():
            (opp_id,) = set(boxes) - {team_id}
            team = teams[team_id]
            team.games += 1
            # Five players' seconds make the game clock's minutes.
            team.minutes += boxes[team_id].seconds / 300
            team.possessions += possessions(boxes[team_id].box, boxes[opp_id].box)
            team.pts += boxes[team_id].pts
            team.opp_pts += boxes[opp_id].pts
            for row, position in team_rows:
                player = team.players.setdefault(row.player_id, _Sums(position=position))
                player.add(row)
    lines = [
        PlayerLine(
            player_id,
            team_id,
            sums.seconds / 60,
            listed_position(sums.position),
            sums.pts,
            sums.fga,
            sums.fg3m,
            sums.fta,
            sums.orb,
            sums.drb,
            sums.ast,
            sums.stl,
            sums.blk,
            sums.tov,
            sums.pf,
        )
        for team_id, team in sorted(teams.items())
        for player_id, sums in sorted(team.players.items())
    ]
    team_lines = {
        team_id: TeamLine(
            team_id, team.games, team.minutes, team.possessions, team.pts, team.opp_pts
        )
        for team_id, team in sorted(teams.items())
    }
    return lines, team_lines


def write_bpm(db: sessionmaker[Session], day: date) -> dict[str, Any]:
    """Make `day`'s `bpm_local` rows every player's BPM over his season's regular-season games
    final through `day`, and return what was written."""
    season = season_label(day)
    with db.begin() as session:
        lines, teams = season_lines(session, season, day)
        rows = snapshot_rows(day, compute(lines, teams))
        changed, removed = replace_snapshot(session, day, SOURCE, rows)
    return {
        "season": season,
        "games": sum(team.games for team in teams.values()) // 2,
        "players": len(rows),
        "changed": changed,
        "removed": removed,
    }
