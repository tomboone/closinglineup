"""`boxscorematchupsv3` → a `MatchupRow` per offensive player and defender (spec §6.2 `matchup`).

Each team lists its players on offense, and under each one the defenders who guarded him.
"""

from typing import Any

from closinglineup.parse.rows import MatchupRow, ParseError

ROOT = "boxScoreMatchups"
SIDES = ("homeTeam", "awayTeam")


def parse_matchups(payload: dict[str, Any]) -> list[MatchupRow]:
    try:
        box: dict[str, Any] = payload[ROOT]
    except KeyError:
        raise ParseError(f"payload has no {ROOT}") from None
    game_id: str = box["gameId"]
    rows: list[MatchupRow] = []
    for side in SIDES:
        offenses: list[dict[str, Any]] = box[side]["players"]
        for offense in offenses:
            defenders: list[dict[str, Any]] = offense.get("matchups") or []
            for defender in defenders:
                stats: dict[str, Any] = defender["statistics"]
                rows.append(
                    MatchupRow.model_validate(
                        dict(
                            game_id=game_id,
                            off_player_id=offense["personId"],
                            def_player_id=defender["personId"],
                            partial_poss=stats.get("partialPossessions"),
                            pts=stats.get("playerPoints"),
                            fgm=stats.get("matchupFieldGoalsMade"),
                            fga=stats.get("matchupFieldGoalsAttempted"),
                            fg3m=stats.get("matchupThreePointersMade"),
                            fg3a=stats.get("matchupThreePointersAttempted"),
                            tov=stats.get("matchupTurnovers"),
                            ast=stats.get("matchupAssists"),
                        )
                    )
                )
    return rows
