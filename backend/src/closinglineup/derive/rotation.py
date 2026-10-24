"""Rotation tiers: each player's `rotation_state` on each team on each of its game dates (spec §6.4,
§7.5, ADR-0003).

`tier` applies the rule to one player-team-date's inputs, the first match winning:

1. **Injured**: his availability is `out` or `doubtful`, which only an injury report gives
   (`derive.availability`);
2. **Starter**: he started at least N of the last K games he played for the team (`TierRule`'s
   `TIER_STARTER_MIN`, 4/5), counted as a rate over the games he has when he has played fewer;
3. **Core**: his minutes share over the team's last 10 games is at least `TIER_CORE_SHARE`, 0.036
   by default: the share is of the team's 240 player-minutes a regulation game, so 8.6 a game;
4. **Fringe**: he played in at least `TIER_FRINGE_PLAYED_RATE` of the team's last 10 games;
5. **Out**: anything else.

So a G League assignment does not decide a tier: an assigned player is Out unless his games for
the team before it make him Fringe or better, and the `g_league` badge says why he is missing.

`compute_rotation_state` writes a date's rows: one per player on each team that played a final game
that date while he was on its roster, the role rows `player_window_stats` has for the date, so it
reads the view as last refreshed. The 10-game inputs are the view's 10-game role window, and the
starter rate is over his last K games played within the role window's season, its `season` row
saying how many of the team's games that spans. A row is short (`is_short`) when its 10-game window
is. Badges are the contract type of his roster span covering the date, unless `standard`, and
`g_league` for a current assignment. `inputs` keeps every number the rule read.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from sqlalchemy import Row, delete, func, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import Base, Game, PlayerGame, RosterMembership
from closinglineup.derive.availability import Availability, availability
from closinglineup.settings import Settings

type Tier = Literal["injured", "starter", "core", "fringe", "out"]

TABLE = "rotation_state"
NATURAL_KEY = ("player_id", "team_id", "game_date")
VALUES = (
    "tier",
    "minutes_share_10",
    "starter_rate_5",
    "played_rate_10",
    "availability",
    "badges",
    "is_short",
    "inputs",
)
INJURED: frozenset[Availability] = frozenset({"out", "doubtful"})
# Contract types that are badges; `standard` is none.
CONTRACT_BADGES = frozenset({"two_way", "ten_day", "exhibit_10"})
G_LEAGUE = "g_league"


@dataclass(frozen=True, slots=True)
class TierRule:
    """Spec §7.5's thresholds: started at least `starter_min` of the last `starter_of` games
    played; a 10-game minutes share of at least `core_share`; played in at least
    `fringe_played_rate` of the last 10 team games."""

    starter_min: int = 4
    starter_of: int = 5
    core_share: float = 0.036
    fringe_played_rate: float = 0.5

    @classmethod
    def from_settings(cls, settings: Settings) -> "TierRule":
        starter_min, starter_of = settings.tier_starter_min
        return cls(
            starter_min, starter_of, settings.tier_core_share, settings.tier_fringe_played_rate
        )


@dataclass(frozen=True, slots=True)
class TierInputs:
    """What the rule reads of a player-team-date. `games_in_window` and `games_played_10` are the
    team games in his 10-game role window and those he played in; `played_5` and `started_5` his
    last `TierRule.starter_of` games played, fewer if he has played fewer, and those he started."""

    availability: Availability
    games_in_window: int
    games_played_10: int
    minutes_share_10: float | None
    played_5: int
    started_5: int

    @property
    def starter_rate_5(self) -> float | None:
        return self.started_5 / self.played_5 if self.played_5 else None

    @property
    def played_rate_10(self) -> float | None:
        return self.games_played_10 / self.games_in_window if self.games_in_window else None

    def as_json(self) -> dict[str, Any]:
        return {
            "availability": self.availability,
            "games_in_window": self.games_in_window,
            "games_played_10": self.games_played_10,
            "minutes_share_10": self.minutes_share_10,
            "played_rate_10": self.played_rate_10,
            "played_5": self.played_5,
            "started_5": self.started_5,
            "starter_rate_5": self.starter_rate_5,
        }


def tier(inputs: TierInputs, rule: TierRule) -> Tier:
    """The first tier of spec §7.5 the inputs meet."""
    if inputs.availability in INJURED:
        return "injured"
    # N of K as a rate, in integers: started / played ≥ N / K.
    if inputs.played_5 and inputs.started_5 * rule.starter_of >= rule.starter_min * inputs.played_5:
        return "starter"
    if inputs.minutes_share_10 is not None and inputs.minutes_share_10 >= rule.core_share:
        return "core"
    played_rate = inputs.played_rate_10
    if played_rate is not None and played_rate >= rule.fringe_played_rate:
        return "fringe"
    return "out"


def compute_rotation_state(db: sessionmaker[Session], day: date, rule: TierRule) -> dict[str, Any]:
    """Make `day`'s `rotation_state` rows exactly those of every player-team with a role window on
    `day`, and return what was written; a re-run over unchanged tables writes nothing."""
    with db.begin() as session:
        windows = _role_windows(session, day)
        played = _last_played(session, day, windows, rule.starter_of)
        contracts = _contracts(session, day, {player_id for player_id, _ in windows})
        rows: list[dict[str, Any]] = []
        for (player_id, team_id), window in sorted(windows.items()):
            games = played.get((player_id, team_id), [])
            inputs = TierInputs(
                availability=availability(session, player_id, team_id, day),
                games_in_window=window.games_in_window,
                games_played_10=window.games_played,
                minutes_share_10=window.minutes_share,
                played_5=len(games),
                started_5=sum(games),
            )
            badges = [
                badge
                for badge in (contracts.get((player_id, team_id)),)
                if badge in CONTRACT_BADGES
            ]
            if inputs.availability == G_LEAGUE:
                badges.append(G_LEAGUE)
            rows.append(
                {
                    "player_id": player_id,
                    "team_id": team_id,
                    "game_date": day,
                    "tier": tier(inputs, rule),
                    "minutes_share_10": inputs.minutes_share_10,
                    "starter_rate_5": inputs.starter_rate_5,
                    "played_rate_10": inputs.played_rate_10,
                    "availability": inputs.availability,
                    "badges": badges,
                    "is_short": window.is_short,
                    "inputs": inputs.as_json(),
                }
            )
        changed, removed = _replace(session, day, rows)
    return {"players": len(rows), "changed": changed, "removed": removed}


@dataclass(frozen=True, slots=True)
class _Window:
    games_in_window: int
    games_played: int
    minutes_share: float | None
    is_short: bool
    # The team games in his role window's season to date.
    season_games: int


def _role_windows(session: Session, day: date) -> dict[tuple[int, int], _Window]:
    """Each player-team's 10-game and season role windows on `day`, from `player_window_stats`."""
    found = session.execute(
        text(
            'SELECT player_id, team_id, "window", games_in_window, games_played, minutes_share, '
            "is_short FROM player_window_stats WHERE game_date = :day AND team_id IS NOT NULL "
            "AND \"window\" IN ('10', 'season')"
        ),
        {"day": day},
    ).all()
    by_window: dict[str, dict[tuple[int, int], Row[Any]]] = defaultdict(dict)
    for row in found:
        by_window[row.window][(row.player_id, row.team_id)] = row
    return {
        key: _Window(
            row.games_in_window,
            row.games_played,
            row.minutes_share,
            row.is_short,
            by_window["season"][key].games_in_window,
        )
        for key, row in by_window["10"].items()
    }


def _last_played(
    session: Session, day: date, windows: dict[tuple[int, int], _Window], count: int
) -> dict[tuple[int, int], list[bool]]:
    """Each player-team's last `count` games played within his role window's season to date,
    newest first, as whether he started each."""
    team_ids = {team_id for _, team_id in windows}
    season = session.scalars(select(Game.season).where(Game.game_date == day).limit(1)).first()
    if season is None:
        return {}
    # The view's team games: each team's dates with a final game, newest first.
    found = session.execute(
        select(Game.home_team_id, Game.away_team_id, Game.game_date).where(
            Game.season == season, Game.game_date <= day, Game.status == "final"
        )
    )
    dated: dict[int, set[date]] = defaultdict(set)
    for home, away, game_date in found:
        dated[home].add(game_date)
        dated[away].add(game_date)
    team_dates = {team_id: sorted(dated[team_id], reverse=True) for team_id in team_ids}
    lines = session.execute(
        select(PlayerGame.player_id, PlayerGame.team_id, Game.game_date, PlayerGame.is_starter)
        .join(Game, Game.game_id == PlayerGame.game_id)
        .where(Game.season == season, Game.game_date <= day, Game.status == "final")
        .where(PlayerGame.team_id.in_(team_ids), PlayerGame.seconds > 0)
        .order_by(Game.game_date.desc())
    )
    played: dict[tuple[int, int], list[bool]] = defaultdict(list)
    for player_id, team_id, game_date, starter in lines:
        window = windows.get((player_id, team_id))
        if window is None or len(played[(player_id, team_id)]) >= count:
            continue
        dates = team_dates[team_id]
        if game_date >= dates[min(window.season_games, len(dates)) - 1]:
            played[(player_id, team_id)].append(starter)
    return played


def _contracts(session: Session, day: date, player_ids: set[int]) -> dict[tuple[int, int], str]:
    """Each player's contract type on his team's roster span covering `day`."""
    found = session.execute(
        select(RosterMembership.player_id, RosterMembership.team_id, RosterMembership.contract_type)
        .where(RosterMembership.player_id.in_(player_ids), RosterMembership.start_date <= day)
        .where(or_(RosterMembership.end_date.is_(None), RosterMembership.end_date >= day))
    )
    return {(player_id, team_id): contract for player_id, team_id, contract in found}


def _replace(session: Session, day: date, rows: list[dict[str, Any]]) -> tuple[int, int]:
    """Make `day`'s rows exactly `rows`: upsert each whose values changed and delete each
    player-team `rows` lack; an unchanged row is not written, `updated_at` included. Returns
    (inserted or updated, deleted). As `impact.replace_snapshot`, on the natural key."""
    table = Base.metadata.tables[TABLE]
    keys = [(row["player_id"], row["team_id"]) for row in rows]
    stale = delete(table).where(table.c.game_date == day)
    if keys:
        stale = stale.where(tuple_(table.c.player_id, table.c.team_id).not_in(keys))
    removed = len(session.execute(stale.returning(table.c.player_id)).all())
    if not rows:
        return 0, removed
    stmt = insert(table).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=NATURAL_KEY,
        set_={**{name: stmt.excluded[name] for name in VALUES}, "updated_at": func.now()},
        where=tuple_(*(table.c[name] for name in VALUES)).is_distinct_from(
            tuple_(*(stmt.excluded[name] for name in VALUES))
        ),
    )
    changed = len(session.execute(stmt.returning(table.c.player_id)).all())
    return changed, removed
