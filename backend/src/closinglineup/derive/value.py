"""The value metrics, VORP and Win Shares, computed here and written to `impact_snapshot` beside
the impact ratings as `source='vorp_local'` and `source='ws_local'` (v2 spec §8.2 to 8.4). They are
value metrics, not ratings: `IMPACT_PREFERENCE` and the player page's impact chart leave them out.

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

Win Shares (§8.2) is not computed yet: the step writes no `ws_local` rows, and removes any a date
has.

A date's rows of each source are replaced on every run: a re-run writes nothing, and a player
without a BPM on the date loses his row.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import Game, ImpactSnapshot, PlayerGame, RosterMembership
from closinglineup.derive.bpm import REGULAR_SEASON_PREFIX
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


def compute_value(db: sessionmaker[Session], day: date) -> dict[str, Any]:
    """Write `day`'s `vorp_local` rows, one per player with a `bpm_local` row on `day`, and clear
    its `ws_local` rows, which stay empty until Win Shares is computed; return what was written."""
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
        _, ws_removed = replace_snapshot(session, day, WS_SOURCE, [])
    return {
        "season": season,
        "players": len(rows),
        "changed": changed,
        "removed": removed + ws_removed,
    }
