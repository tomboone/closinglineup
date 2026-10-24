"""`GET /api/games/{game_id}`: a game's page (spec §8).

- the **stints** are both teams' stints, one row per player on the floor in each, so the timeline
  draws a row per player; every stint, garbage time included, each marked as `derive` classified
  it;
- each team's **lineups** are its fives in the game by offensive possessions, garbage time left
  out unless `?garbage_time=include` as on the team page, a stint not yet classified counting
  either way. A stint's defensive possessions are the other team's view of it;
- each team's **closing** lineup is its last stint's five, when the game was decided by
  `CLOSE_MARGIN` or fewer;
- **garbage_time_from** is the first garbage-time stint's start.
"""

from collections import defaultdict
from collections.abc import Sequence
from typing import cast

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from closinglineup.api.deps import SessionDep
from closinglineup.api.routers.teams import (
    CLOSE_MARGIN,
    StintRow,
    Totals,
    lineup_members,
    player_names,
    player_ref,
)
from closinglineup.api.schemas import (
    GameLineup,
    GamePage,
    GamePlayer,
    GameStatus,
    GameTeam,
    GarbageTime,
    GarbageTimeMarker,
    PlayerStint,
)
from closinglineup.db.models import Game, PlayerGame, Stint
from closinglineup.teams import by_nba_team_id

router = APIRouter()


@router.get("/api/games/{game_id}")
def game_page(game_id: str, session: SessionDep, garbage_time: GarbageTime = "exclude") -> GamePage:
    game = session.get(Game, game_id)
    if game is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown game: {game_id}")
    stints = list(
        session.scalars(
            select(Stint)
            .where(Stint.game_id == game_id)
            .order_by(Stint.period, Stint.start_sec, Stint.team_id)
        )
    )
    lines = list(
        session.scalars(
            select(PlayerGame)
            .where(PlayerGame.game_id == game_id, PlayerGame.seconds > 0)
            .order_by(PlayerGame.is_starter.desc(), PlayerGame.seconds.desc())
        )
    )
    names = player_names(
        session,
        {line.player_id for line in lines}
        | {p for stint in stints for p in lineup_members(stint.lineup_key)},
    )
    margin = None
    if game.home_score is not None and game.away_score is not None:
        margin = game.home_score - game.away_score
    close = margin is not None and abs(margin) <= CLOSE_MARGIN
    garbage = next((stint for stint in stints if stint.is_garbage_time), None)
    return GamePage(
        game_id=game.game_id,
        game_date=game.game_date,
        status=cast(GameStatus, game.status),
        garbage_time=garbage_time,
        home=_team(game.home_team_id, game.home_score, stints, lines, names, close, garbage_time),
        away=_team(game.away_team_id, game.away_score, stints, lines, names, close, garbage_time),
        stints=[
            PlayerStint(
                player_id=player_id,
                team_id=stint.team_id,
                period=stint.period,
                start_sec=stint.start_sec,
                end_sec=stint.end_sec,
                is_garbage_time=stint.is_garbage_time,
            )
            for stint in stints
            for player_id in lineup_members(stint.lineup_key)
        ],
        garbage_time_from=(
            GarbageTimeMarker(period=garbage.period, start_sec=garbage.start_sec)
            if garbage is not None
            else None
        ),
    )


def _team(
    team_id: int,
    score: int | None,
    stints: Sequence[Stint],
    lines: Sequence[PlayerGame],
    names: dict[int, str],
    close: bool,
    garbage_time: GarbageTime,
) -> GameTeam:
    own = [stint for stint in stints if stint.team_id == team_id]
    closing = None
    if close and own:
        last = max(own, key=lambda stint: (stint.period, stint.end_sec))
        closing = [player_ref(p, names) for p in lineup_members(last.lineup_key)]
    return GameTeam(
        team_id=team_id,
        tricode=by_nba_team_id(team_id).tricode,
        score=score,
        players=[
            GamePlayer(
                player=player_ref(line.player_id, names),
                is_starter=line.is_starter,
                seconds=line.seconds,
            )
            for line in lines
            if line.team_id == team_id
        ],
        lineups=_lineups(team_id, stints, names, garbage_time),
        closing=closing,
    )


def _lineups(
    team_id: int, stints: Sequence[Stint], names: dict[int, str], garbage_time: GarbageTime
) -> list[GameLineup]:
    """The team's fives in the game, the most offensive possessions first, then by player IDs."""
    opp_possessions = {
        (stint.period, stint.start_sec): stint.possessions
        for stint in stints
        if stint.team_id != team_id
    }
    totals: dict[str, Totals] = defaultdict(Totals)
    for stint in stints:
        if stint.team_id != team_id:
            continue
        if garbage_time == "exclude" and stint.is_garbage_time is True:
            continue
        totals[stint.lineup_key].add(
            StintRow(
                stint.game_id,
                stint.lineup_key,
                stint.possessions,
                opp_possessions.get((stint.period, stint.start_sec), 0),
                stint.pts_for,
                stint.pts_against,
            )
        )
    ranked = sorted(
        totals.items(), key=lambda item: (-item[1].possessions, lineup_members(item[0]))
    )
    return [
        GameLineup(
            players=[player_ref(p, names) for p in lineup_members(key)],
            possessions=total.possessions,
            opp_possessions=total.opp_possessions,
            pts_for=total.pts_for,
            pts_against=total.pts_against,
            net_rating=total.net_rating,
        )
        for key, total in ranked
    ]
