"""The value metrics, VORP and Win Shares, computed here and written to `impact_snapshot` beside
the impact metrics as `source='vorp_local'` and `source='ws_local'` (v2 spec §8.2 to 8.4). They are
value metrics, not impact metrics: `IMPACT_PREFERENCE` and the player page's impact chart leave
them out.

VORP: Basketball Reference's, "About Box Plus/Minus (BPM)",
https://www.basketball-reference.com/about/bpm2.html: the points per 100 team possessions a player
added over a replacement-level one (a BPM of -2.0), scaled by his share of his team's floor time
and the share of the season his team has played,

    VORP = (BPM + 2.0) * (5 * minutes share) * (team games / 82)

BPM is his `bpm_local` snapshot on the date (`derive.bpm`), so `value` runs after `bpm`. Minutes
share is the glossary's (v1 spec §7.1) over his season production window: his minutes over his
teams' available player-minutes (five players over the game's length, overtime included) in the
games he was on their roster for, so `5 *` makes it his fraction of the game clock. Team games are
those games. Both are over the season's regular-season games final through the date, as `bpm`'s
are; a traded player's count every team's games he was rostered for, one a date, as the production
window does, and the published season row (`TOT`) does. A game he played counts as rostered
whatever the roster spans say, so every player with a BPM has a share.

Win Shares: Basketball Reference's, "Calculating Win Shares",
https://www.basketball-reference.com/about/ws.html, with the individual offensive and defensive
ratings it takes from Dean Oliver, *Basketball on Paper* (2004). Computed per player-team line
over the season's regular-season games final through the date, each from the player's totals on
that team and the team's in the games it played (`derive.bpm.season_lines`), as the published
per-team row is; a player's row is the sum of his teams', as the published season row (`TOT`) is.

A player-team line's totals are `MP, FGM, FGA, FG3M, FTM, FTA, ORB, DRB, AST, STL, BLK, TOV, PF,
PTS`; its team's carry a `Tm_` prefix and its opponents' in those games an `Opp_` prefix, each its
players' sums (`TeamLine.box`, `TeamLine.opp_box`), points too rather than the schedule's score.
`Tm_MP` is the five players' minutes together, five times the game clock (`TeamLine.minutes`),
and `Opp_MP` equals it, the same games' clocks.
`Tm_Poss` is the team's possessions, Basketball Reference's two-sided estimate over each game's box
totals (`derive.bpm.possessions`), not counted from `stint`, so the result is comparable with the
published one. `Lg_PTS`, `Lg_Poss` and `Lg_G`, the team-games, are the sums over every team with
possessions.

    Tm_Pace = 48 * Tm_Poss / (Tm_MP / 5)
    Lg_Pace = 48 * Lg_Poss / sum(Tm_MP / 5)
    Marginal_Points_Per_Win = 0.32 * (Lg_PTS / Lg_G) * (Tm_Pace / Lg_Pace)

Offensive Win Shares, from Oliver's points produced and offensive possessions:

    Team_Scoring_Poss = Tm_FGM + (1 - (1 - Tm_FTM / Tm_FTA)^2) * Tm_FTA * 0.4
    Team_ORB% = Tm_ORB / (Tm_ORB + Opp_DRB)
    Team_Play% = Team_Scoring_Poss / (Tm_FGA + Tm_FTA * 0.4 + Tm_TOV)
    Team_ORB_Weight = ((1 - Team_ORB%) * Team_Play%)
                      / ((1 - Team_ORB%) * Team_Play% + Team_ORB% * (1 - Team_Play%))
    qAST = (MP / (Tm_MP / 5)) * 1.14 * ((Tm_AST - AST) / Tm_FGM)
           + ((Tm_AST / Tm_MP * MP * 5 - AST) / (Tm_FGM / Tm_MP * MP * 5 - FGM))
             * (1 - MP / (Tm_MP / 5))
    FG_Part = FGM * (1 - 0.5 * ((PTS - FTM) / (2 * FGA)) * qAST)
    AST_Part = 0.5 * (((Tm_PTS - Tm_FTM) - (PTS - FTM)) / (2 * (Tm_FGA - FGA))) * AST
    FT_Part = (1 - (1 - FTM / FTA)^2) * 0.4 * FTA
    ORB_Part = ORB * Team_ORB_Weight * Team_Play%
    ScPoss = (FG_Part + AST_Part + FT_Part)
             * (1 - (Tm_ORB / Team_Scoring_Poss) * Team_ORB_Weight * Team_Play%) + ORB_Part
    FGxPoss = (FGA - FGM) * (1 - 1.07 * Team_ORB%)
    FTxPoss = (1 - FTM / FTA)^2 * 0.4 * FTA
    TotPoss = ScPoss + FGxPoss + FTxPoss + TOV

    PProd_FG_Part = 2 * (FGM + 0.5 * FG3M) * (1 - 0.5 * ((PTS - FTM) / (2 * FGA)) * qAST)
    PProd_AST_Part = 2 * ((Tm_FGM - FGM + 0.5 * (Tm_FG3M - FG3M)) / (Tm_FGM - FGM))
                     * 0.5 * (((Tm_PTS - Tm_FTM) - (PTS - FTM)) / (2 * (Tm_FGA - FGA))) * AST
    PProd_ORB_Part = ORB * Team_ORB_Weight * Team_Play% * (Tm_PTS / Team_Scoring_Poss)
    PProd = (PProd_FG_Part + PProd_AST_Part + FTM)
            * (1 - (Tm_ORB / Team_Scoring_Poss) * Team_ORB_Weight * Team_Play%) + PProd_ORB_Part

    Marginal_Offense = PProd - 0.92 * (Lg_PTS / Lg_Poss) * TotPoss
    OWS = Marginal_Offense / Marginal_Points_Per_Win

Defensive Win Shares, from Oliver's individual defensive rating over the team's stops:

    DOR% = Opp_ORB / (Opp_ORB + Tm_DRB)
    DFG% = Opp_FGM / Opp_FGA
    FMwt = (DFG% * (1 - DOR%)) / (DFG% * (1 - DOR%) + (1 - DFG%) * DOR%)
    Stops1 = STL + BLK * FMwt * (1 - 1.07 * DOR%) + DRB * (1 - FMwt)
    Stops2 = (((Opp_FGA - Opp_FGM - Tm_BLK) / Tm_MP) * FMwt * (1 - 1.07 * DOR%)
              + (Opp_TOV - Tm_STL) / Tm_MP) * MP
             + (PF / Tm_PF) * 0.4 * Opp_FTA * (1 - Opp_FTM / Opp_FTA)^2
    Stop% = ((Stops1 + Stops2) * Opp_MP) / (Tm_Poss * MP)
    Team_DRtg = 100 * Opp_PTS / Tm_Poss
    D_Pts_per_ScPoss = Opp_PTS / (Opp_FGM + (1 - (1 - Opp_FTM / Opp_FTA)^2) * Opp_FTA * 0.4)
    DRtg = Team_DRtg + 0.2 * (100 * D_Pts_per_ScPoss * (1 - Stop%) - Team_DRtg)

    Marginal_Defense = (MP / Tm_MP) * Tm_Poss * (1.08 * (Lg_PTS / Lg_Poss) - DRtg / 100)
    DWS = Marginal_Defense / Marginal_Points_Per_Win

    WS = OWS + DWS

A ratio whose denominator is zero is zero: no free throws make the free-throw terms zero, no field
goal attempts the `(PTS - FTM) / (2 * FGA)` factor, no team fouls the foul share. A team with no
possessions, scoring possessions, field goal or free throw attempts, or opponents' field goal or
free throw attempts (one with no real box yet) gives its players no Win Shares, and nor does
a line of no minutes.

Where this departs from the published values: `Tm_TOV` and `Opp_TOV` are the players' sums, since
turnovers charged to a team are not in `player_game` (as in `derive.bpm`); they enter only
`Team_Play%` and the possession estimate, and the league's context moves with them, so a season's
WS moves by hundredths. `Tm_MP` is the players' seconds summed, not rounded to minutes. A game with
rows for only one side is left out of both teams' totals (`season_lines`). The published values are
checked in `tests/test_value.py`.

A player has a Win Shares row once he has played `bpm.MIN_MINUTES`, the players `bpm_local` rates;
his minutes are its basis.

A date's rows of each source are replaced on every run: a re-run writes nothing, and a player
without a BPM, or without a minute, on the date loses his row.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import Game, ImpactSnapshot, PlayerGame, RosterMembership
from closinglineup.derive.bpm import (
    MIN_MINUTES,
    REGULAR_SEASON_PREFIX,
    PlayerLine,
    TeamLine,
    season_lines,
)
from closinglineup.derive.bpm import SOURCE as BPM_SOURCE
from closinglineup.impact import ImpactSnapshotRow, ImpactSource, replace_snapshot
from closinglineup.season import season_label

VORP_SOURCE: ImpactSource = "vorp_local"
WS_SOURCE: ImpactSource = "ws_local"
# A replacement-level player's BPM, and the season's length in team games.
REPLACEMENT_BPM = -2.0
SEASON_GAMES = 82
# A regulation game's length in seconds, and an overtime's.
REGULATION_SECONDS = 2880
OVERTIME_SECONDS = 300
# Win Shares' constants: the share of league points per game that is a win's margin, and the
# league points per possession a replacement player's offense produces (0.92 of the league's) and
# its defense allows (1.08).
POINTS_PER_WIN_FACTOR = 0.32
REPLACEMENT_OFFENSE = 0.92
REPLACEMENT_DEFENSE = 1.08


@dataclass(frozen=True, slots=True)
class PlayerSeason:
    """A player's season to date over the team games he was rostered for: his seconds, his teams'
    available player-seconds in them, and how many there were."""

    player_id: int
    seconds: int
    team_seconds: int
    team_games: int

    @property
    def minutes_share(self) -> float:
        return self.seconds / self.team_seconds if self.team_seconds else 0.0


def vorp(bpm: float, season: PlayerSeason) -> float:
    """Value over replacement player, from his BPM and his season to date."""
    return (bpm - REPLACEMENT_BPM) * 5 * season.minutes_share * season.team_games / SEASON_GAMES


def available_seconds(seconds: int) -> int:
    """A team's available player-seconds in a game whose players played `seconds` together: five
    players over 48 minutes plus each overtime, the overtimes read off the players' seconds, as
    the window view's `team_box` reads them."""
    overtimes = round(max(seconds / 5 - REGULATION_SECONDS, 0) / OVERTIME_SECONDS)
    return 5 * (REGULATION_SECONDS + OVERTIME_SECONDS * overtimes)


def vorp_rows(
    day: date, bpms: Mapping[int, float], seasons: Mapping[int, PlayerSeason]
) -> list[ImpactSnapshotRow]:
    """`day`'s `vorp_local` rows: one per player with a BPM, his minutes the basis, rounded as
    `bpm_local`'s are so a re-run writes nothing."""
    rows: list[ImpactSnapshotRow] = []
    for player_id, bpm in sorted(bpms.items()):
        season = seasons.get(player_id, PlayerSeason(player_id, 0, 0, 0))
        rows.append(
            ImpactSnapshotRow(
                day,
                player_id,
                VORP_SOURCE,
                round(vorp(bpm, season), 3),
                None,
                None,
                round(season.seconds / 60, 1),
            )
        )
    return rows


def _spans_cover(spans: Iterable[tuple[date, date | None]], day: date) -> bool:
    return any(start <= day and (end is None or day <= end) for start, end in spans)


def player_seasons(
    session: Session, season: str, through: date, player_ids: Iterable[int]
) -> dict[int, PlayerSeason]:
    """Each of `player_ids`' season to date: the team games of `season`'s regular season final
    through `through` that he was rostered for, or played in, one a date (the one he has a box
    line for, should two of his teams both play), with his seconds and his team's available
    player-seconds in each."""
    wanted = set(player_ids)
    games = session.execute(
        select(Game.game_id, Game.game_date, Game.home_team_id, Game.away_team_id)
        .where(
            Game.season == season,
            Game.game_date <= through,
            Game.status == "final",
            Game.game_id.like(f"{REGULAR_SEASON_PREFIX}%"),
        )
        .order_by(Game.game_date, Game.game_id)
    ).all()
    # Each team's games, one a date: the first by game ID, as the window view keys them.
    team_games: dict[int, dict[date, str]] = defaultdict(dict)
    for game_id, game_date, home, away in games:
        for team_id in (home, away):
            team_games[team_id].setdefault(game_date, game_id)
    box = session.execute(
        select(PlayerGame.game_id, PlayerGame.team_id, PlayerGame.player_id, PlayerGame.seconds)
        .join(Game, Game.game_id == PlayerGame.game_id)
        .where(
            Game.season == season,
            Game.game_date <= through,
            Game.status == "final",
            Game.game_id.like(f"{REGULAR_SEASON_PREFIX}%"),
        )
    ).all()
    team_played: dict[tuple[str, int], int] = defaultdict(int)
    lines: dict[tuple[str, int, int], int] = {}
    for game_id, team_id, player_id, seconds in box:
        team_played[game_id, team_id] += seconds
        if player_id in wanted:
            lines[game_id, team_id, player_id] = seconds
    spans: dict[tuple[int, int], list[tuple[date, date | None]]] = defaultdict(list)
    for player_id, team_id, start, end in session.execute(
        select(
            RosterMembership.player_id,
            RosterMembership.team_id,
            RosterMembership.start_date,
            RosterMembership.end_date,
        ).where(RosterMembership.player_id.in_(wanted))
    ):
        spans[player_id, team_id].append((start, end))
    # Each player's candidate games by date: (no box line, team ID, game ID), so the least is the
    # one he has a line for.
    candidates: dict[int, dict[date, set[tuple[bool, int, str]]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for (player_id, team_id), covered in spans.items():
        for game_date, game_id in team_games.get(team_id, {}).items():
            if _spans_cover(covered, game_date):
                missing = (game_id, team_id, player_id) not in lines
                candidates[player_id][game_date].add((missing, team_id, game_id))
    by_game = {game_id: game_date for game_id, game_date, _, _ in games}
    for (game_id, team_id, player_id), seconds in lines.items():
        if seconds > 0:
            candidates[player_id][by_game[game_id]].add((False, team_id, game_id))
    seasons: dict[int, PlayerSeason] = {}
    for player_id, dates in candidates.items():
        chosen = [min(options) for options in dates.values()]
        seasons[player_id] = PlayerSeason(
            player_id,
            sum(lines.get((game_id, team_id, player_id), 0) for _, team_id, game_id in chosen),
            sum(
                available_seconds(team_played.get((game_id, team_id), 0))
                for _, team_id, game_id in chosen
            ),
            len(chosen),
        )
    return seasons


@dataclass(frozen=True, slots=True)
class LeagueContext:
    """The league's points, possessions, team-games and pace through the date, over the teams
    with possessions."""

    pts: int
    possessions: float
    team_games: int
    pace: float

    @property
    def points_per_possession(self) -> float:
        return self.pts / self.possessions


@dataclass(frozen=True, slots=True)
class TeamWinShares:
    """A player's offensive and defensive Win Shares on one team."""

    player_id: int
    team_id: int
    minutes: float
    ows: float
    dws: float


def _div(numerator: float, denominator: float) -> float:
    """A ratio, zero when its denominator is."""
    return numerator / denominator if denominator else 0.0


def _rated(team: TeamLine) -> bool:
    """Whether the team's box carries everything a line's formulas divide by."""
    box, opp = team.box, team.opp_box
    return team.possessions > 0 and team.minutes > 0 and min(box.fga, box.fta, opp.fga, opp.fta) > 0


def _pace(possessions: float, minutes: float) -> float:
    return 48 * possessions / minutes


def league_context(teams: Iterable[TeamLine]) -> LeagueContext | None:
    """The league's context over `teams` that have possessions; None when none has."""
    rated = [team for team in teams if team.possessions > 0 and team.minutes > 0]
    if not rated:
        return None
    possessions = sum(team.possessions for team in rated)
    return LeagueContext(
        sum(team.box.pts for team in rated),
        possessions,
        sum(team.games for team in rated),
        _pace(possessions, sum(team.minutes for team in rated)),
    )


def win_shares(line: PlayerLine, team: TeamLine, league: LeagueContext) -> TeamWinShares | None:
    """The player's Win Shares on `team`, by the formulas in the module docstring; None for a
    line of no minutes or a team without the box they divide by."""
    if line.minutes <= 0 or not _rated(team):
        return None
    tm, opp = team.box, team.opp_box
    mp, tm_mp = line.minutes, team.minutes * 5
    fgm, fga, fg3m, ftm, fta = line.fgm, line.fga, line.fg3m, line.ftm, line.fta
    tm_poss = team.possessions
    team_scoring_poss = tm.fgm + (1 - (1 - tm.ftm / tm.fta) ** 2) * tm.fta * 0.4
    if team_scoring_poss <= 0:
        return None
    points_per_win = (
        POINTS_PER_WIN_FACTOR
        * (league.pts / league.team_games)
        * (_pace(tm_poss, team.minutes) / league.pace)
    )
    if points_per_win <= 0:
        return None

    # Offense.
    team_orb_pct = _div(tm.orb, tm.orb + opp.drb)
    team_play_pct = team_scoring_poss / (tm.fga + tm.fta * 0.4 + tm.tov)
    team_orb_weight = _div(
        (1 - team_orb_pct) * team_play_pct,
        (1 - team_orb_pct) * team_play_pct + team_orb_pct * (1 - team_play_pct),
    )
    floor_share = mp / (tm_mp / 5)
    q_ast = floor_share * 1.14 * _div(tm.ast - line.ast, tm.fgm) + _div(
        tm.ast / tm_mp * mp * 5 - line.ast, tm.fgm / tm_mp * mp * 5 - fgm
    ) * (1 - floor_share)
    shot_factor = 1 - 0.5 * _div(line.pts - ftm, 2 * fga) * q_ast
    assisted = _div((tm.pts - tm.ftm) - (line.pts - ftm), 2 * (tm.fga - fga))
    ft_miss_sq = (1 - _div(ftm, fta)) ** 2
    fg_part = fgm * shot_factor
    ast_part = 0.5 * assisted * line.ast
    ft_part = (1 - ft_miss_sq) * 0.4 * fta if fta else 0.0
    orb_part = line.orb * team_orb_weight * team_play_pct
    orb_discount = 1 - (tm.orb / team_scoring_poss) * team_orb_weight * team_play_pct
    sc_poss = (fg_part + ast_part + ft_part) * orb_discount + orb_part
    fgx_poss = (fga - fgm) * (1 - 1.07 * team_orb_pct)
    ftx_poss = ft_miss_sq * 0.4 * fta if fta else 0.0
    tot_poss = sc_poss + fgx_poss + ftx_poss + line.tov
    pprod_fg_part = 2 * (fgm + 0.5 * fg3m) * shot_factor
    pprod_ast_part = (
        2 * _div(tm.fgm - fgm + 0.5 * (tm.fg3m - fg3m), tm.fgm - fgm) * 0.5 * assisted * line.ast
    )
    pprod_orb_part = orb_part * (tm.pts / team_scoring_poss)
    pprod = (pprod_fg_part + pprod_ast_part + ftm) * orb_discount + pprod_orb_part
    marginal_offense = pprod - REPLACEMENT_OFFENSE * league.points_per_possession * tot_poss

    # Defense.
    dor_pct = _div(opp.orb, opp.orb + tm.drb)
    dfg_pct = opp.fgm / opp.fga
    fm_wt = _div(dfg_pct * (1 - dor_pct), dfg_pct * (1 - dor_pct) + (1 - dfg_pct) * dor_pct)
    opp_ft_miss_sq = (1 - opp.ftm / opp.fta) ** 2
    stops1 = line.stl + line.blk * fm_wt * (1 - 1.07 * dor_pct) + line.drb * (1 - fm_wt)
    stops2 = (
        (opp.fga - opp.fgm - tm.blk) / tm_mp * fm_wt * (1 - 1.07 * dor_pct)
        + (opp.tov - tm.stl) / tm_mp
    ) * mp + _div(line.pf, tm.pf) * 0.4 * opp.fta * opp_ft_miss_sq
    stop_pct = (stops1 + stops2) * tm_mp / (tm_poss * mp)
    team_drtg = 100 * opp.pts / tm_poss
    d_pts_per_sc_poss = _div(opp.pts, opp.fgm + (1 - opp_ft_miss_sq) * opp.fta * 0.4)
    drtg = team_drtg + 0.2 * (100 * d_pts_per_sc_poss * (1 - stop_pct) - team_drtg)
    marginal_defense = (
        (mp / tm_mp) * tm_poss * (REPLACEMENT_DEFENSE * league.points_per_possession - drtg / 100)
    )

    return TeamWinShares(
        line.player_id,
        team.team_id,
        mp,
        marginal_offense / points_per_win,
        marginal_defense / points_per_win,
    )


def season_win_shares(
    lines: Iterable[PlayerLine], teams: Mapping[int, TeamLine]
) -> list[TeamWinShares]:
    """Every player-team line's Win Shares over the lines of `teams`, the league's context
    theirs; a line of a team not in `teams` has none."""
    league = league_context(teams.values())
    if league is None:
        return []
    return [
        share
        for line in lines
        if line.team_id in teams
        and (share := win_shares(line, teams[line.team_id], league)) is not None
    ]


def ws_rows(day: date, shares: Iterable[TeamWinShares]) -> list[ImpactSnapshotRow]:
    """`day`'s `ws_local` rows: one per player with `MIN_MINUTES` or more over his teams, their
    Win Shares summed and his minutes the basis, rounded so a re-run writes nothing."""
    by_player: dict[int, list[TeamWinShares]] = defaultdict(list)
    for share in shares:
        by_player[share.player_id].append(share)
    rows: list[ImpactSnapshotRow] = []
    for player_id, played in sorted(by_player.items()):
        minutes = sum(share.minutes for share in played)
        if minutes < MIN_MINUTES:
            continue
        ows = sum(share.ows for share in played)
        dws = sum(share.dws for share in played)
        rows.append(
            ImpactSnapshotRow(
                day,
                player_id,
                WS_SOURCE,
                round(ows + dws, 3),
                round(ows, 3),
                round(dws, 3),
                round(minutes, 1),
            )
        )
    return rows


def compute_value(db: sessionmaker[Session], day: date) -> dict[str, Any]:
    """Write `day`'s `vorp_local` rows, one per player with a `bpm_local` row on `day`, and its
    `ws_local` rows, one per player with a minute or more in the season through `day`; return
    what was written."""
    season = season_label(day)
    with db.begin() as session:
        bpms = dict(
            session.execute(
                select(ImpactSnapshot.player_id, ImpactSnapshot.value).where(
                    ImpactSnapshot.date == day, ImpactSnapshot.source == BPM_SOURCE
                )
            ).all()
        )
        seasons = player_seasons(session, season, day, bpms)
        rows = vorp_rows(day, bpms, seasons)
        changed, removed = replace_snapshot(session, day, VORP_SOURCE, rows)
        lines, teams = season_lines(session, season, day)
        ws = ws_rows(day, season_win_shares(lines, teams))
        ws_changed, ws_removed = replace_snapshot(session, day, WS_SOURCE, ws)
    return {
        "season": season,
        "players": len(rows),
        "ws_players": len(ws),
        "changed": changed + ws_changed,
        "removed": removed + ws_removed,
    }
