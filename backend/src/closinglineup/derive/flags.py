"""Flags: what is worth a look about a player on a team on one of its game dates (spec §6.4, §7.7,
ADR-0003).

`compute_flags` writes a date's `player_flag` rows for each player-team with a `rotation_state` row
on the date, so it runs after `derive.events.compute_events` has written the date's events. Each
flag is a function of the player-team's `FlagInputs`, its thresholds in `FlagRule`, and keeps the
numbers that raised it in `detail`:

- `minutes_cliff`: his 5-game minutes share is below `cliff_ratio` (60%) of his 20-game share,
  unless he is Injured, whose minutes fall for a reason already known;
- `usage_up_efficiency_down`: over his 10-game production window, his usage is up at least
  `usage_up` (3 points) on his season's and his true shooting down at least `ts_down` (4 points);
- `bleeding`: his on-court net rating over his 10-game role window is at most `bleeding_net` (-5)
  on at least `bleeding_possessions` (200) possessions, while the team's with him off the floor
  over the same games is positive;
- `earning_trust`: his minutes share rises across three consecutive non-overlapping 5-game role
  windows, the latest ending on the date, each higher than the one before; all three must lie in
  his current spell with the team this season;
- `yo_yo`: at least `yo_yo_events` (3) tier changes on the team in its last `yo_yo_games` (20)
  final games this season, the date included: rotation events with a `from_tier`, on a date whose
  `rotation_state` row is not short. An arrival, and a change raised while his role window was
  still filling, do not count, so a mid-season signing yo-yos only once his window is full;
- `contract_pressure`: in the latest weekly `contract` snapshot on or before the date, a contract
  with the team whose `guarantee_date` is within `guarantee_days` (30) of the date, the date
  itself included, and whose `guaranteed_after` exceeds `guaranteed`, or is NULL, an unpriced
  minimum deal; or a two-way contract (`roster_membership`) within `two_way_margin` (5) of
  `two_way_limit`, the regular-season games, NBA Cup knockouts included, he has been active for
  (a box score line he played in or sat out by the coach's decision) with the team since his
  two-way span started, this season. The limit is not prorated for a mid-season signing.

A threshold the spec states inclusively ("at least", "within") is inclusive; the cliff's "below"
is strict. A comparison treats values within `EPSILON` of each other as equal, so a boundary the
spec states exactly holds however the window's sums round. A window's metric that is NULL raises no
flag. A date's flags are deleted and rewritten on every run.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

from sqlalchemy import delete, func, insert, or_, select, text, tuple_
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import (
    Contract,
    Game,
    PlayerFlag,
    PlayerGame,
    RosterMembership,
    RotationEvent,
    RotationState,
)

EPSILON = 1e-9
# Box-score lines that put him on the active list: he played, or sat by the coach's decision.
ACTIVE_DNP_REASONS = frozenset({"cd"})
# Phases of the regular season: an NBA Cup knockout is one of its games, but for the final.
REGULAR_SEASON = ("regular", "cup_knockout")


@dataclass(frozen=True, slots=True)
class FlagRule:
    """Spec §7.7's thresholds. Rates are fractions, as `player_window_stats`'s are (0.03 is 3
    points); `two_way_limit` is the 2023 CBA's 50 regular-season games a two-way player may be
    active for."""

    cliff_ratio: float = 0.6
    usage_up: float = 0.03
    ts_down: float = 0.04
    bleeding_net: float = -5.0
    bleeding_possessions: int = 200
    yo_yo_events: int = 3
    yo_yo_games: int = 20
    guarantee_days: int = 30
    two_way_limit: int = 50
    two_way_margin: int = 5


@dataclass(frozen=True, slots=True)
class Guarantee:
    """His contract's next guarantee: when, what is guaranteed now and what will be (None for an
    unpriced minimum deal)."""

    guarantee_date: date
    guaranteed: int
    guaranteed_after: int | None


@dataclass(frozen=True, slots=True)
class TierChange:
    """A tier change on the team, a rotation event other than an arrival: when, from and to."""

    game_date: date
    from_tier: str
    to_tier: str


@dataclass(frozen=True, slots=True)
class FlagInputs:
    """Everything the flags read for one player-team-date; None, or empty, where there is nothing
    to read."""

    tier: str = "out"
    share_5: float | None = None
    share_20: float | None = None
    usage_10: float | None = None
    usage_delta: float | None = None
    ts_10: float | None = None
    ts_delta: float | None = None
    net_10: float | None = None
    on_off_10: float | None = None
    possessions_10: int | None = None
    # His 5-game minutes shares in three consecutive non-overlapping windows, the oldest first.
    trust_shares: tuple[float | None, ...] = ()
    changes: tuple[TierChange, ...] = ()
    guarantee: Guarantee | None = None
    # The regular-season games he has been active for on his current two-way contract.
    two_way_active: int | None = None


def _at_least(value: float, threshold: float) -> bool:
    return value >= threshold - EPSILON


def _at_most(value: float, threshold: float) -> bool:
    return value <= threshold + EPSILON


def _round(value: float) -> float:
    return round(value, 4)


def minutes_cliff(inputs: FlagInputs, rule: FlagRule) -> dict[str, Any] | None:
    share_5, share_20 = inputs.share_5, inputs.share_20
    if inputs.tier == "injured" or share_5 is None or not share_20:
        return None
    if _at_least(share_5, rule.cliff_ratio * share_20):
        return None
    return {
        "share_5": _round(share_5),
        "share_20": _round(share_20),
        "ratio": _round(share_5 / share_20),
    }


def usage_up_efficiency_down(inputs: FlagInputs, rule: FlagRule) -> dict[str, Any] | None:
    usage, ts = inputs.usage_delta, inputs.ts_delta
    if usage is None or ts is None:
        return None
    if not (_at_least(usage, rule.usage_up) and _at_most(ts, -rule.ts_down)):
        return None
    return {
        "usg_pct_10": None if inputs.usage_10 is None else _round(inputs.usage_10),
        "usg_pct_delta_season": _round(usage),
        "ts_pct_10": None if inputs.ts_10 is None else _round(inputs.ts_10),
        "ts_pct_delta_season": _round(ts),
    }


def bleeding(inputs: FlagInputs, rule: FlagRule) -> dict[str, Any] | None:
    net, on_off, possessions = inputs.net_10, inputs.on_off_10, inputs.possessions_10
    if net is None or on_off is None or possessions is None:
        return None
    # On/off is his on-court net less the team's with him off the floor.
    off = net - on_off
    if not (
        _at_most(net, rule.bleeding_net)
        and possessions >= rule.bleeding_possessions
        and off > EPSILON
    ):
        return None
    return {"on_court_net": _round(net), "possessions": possessions, "off_court_net": _round(off)}


def earning_trust(inputs: FlagInputs, rule: FlagRule) -> dict[str, Any] | None:
    shares = inputs.trust_shares
    if len(shares) != 3 or any(share is None for share in shares):
        return None
    oldest, middle, latest = (share or 0.0 for share in shares)
    if not (middle > oldest + EPSILON and latest > middle + EPSILON):
        return None
    return {"shares": [_round(oldest), _round(middle), _round(latest)]}


def yo_yo(inputs: FlagInputs, rule: FlagRule) -> dict[str, Any] | None:
    if len(inputs.changes) < rule.yo_yo_events:
        return None
    return {
        "events": len(inputs.changes),
        "changes": [
            {
                "game_date": change.game_date.isoformat(),
                "from_tier": change.from_tier,
                "to_tier": change.to_tier,
            }
            for change in inputs.changes
        ],
    }


def contract_pressure(inputs: FlagInputs, rule: FlagRule, day: date) -> dict[str, Any] | None:
    guarantee = inputs.guarantee
    if guarantee is not None:
        days = (guarantee.guarantee_date - day).days
        rises = (
            guarantee.guaranteed_after is None or guarantee.guaranteed_after > guarantee.guaranteed
        )
        if 0 <= days <= rule.guarantee_days and rises:
            return {
                "reason": "guarantee",
                "guarantee_date": guarantee.guarantee_date.isoformat(),
                "days": days,
                "guaranteed": guarantee.guaranteed,
                "guaranteed_after": guarantee.guaranteed_after,
            }
    active = inputs.two_way_active
    if active is not None and active >= rule.two_way_limit - rule.two_way_margin:
        return {
            "reason": "two_way",
            "active_games": active,
            "limit": rule.two_way_limit,
            "remaining": rule.two_way_limit - active,
        }
    return None


def flags(inputs: FlagInputs, rule: FlagRule, day: date) -> dict[str, dict[str, Any]]:
    """The flags `inputs` raise on `day`, each with its `detail`, in spec §7.7's order."""
    raised = {
        "minutes_cliff": minutes_cliff(inputs, rule),
        "usage_up_efficiency_down": usage_up_efficiency_down(inputs, rule),
        "bleeding": bleeding(inputs, rule),
        "earning_trust": earning_trust(inputs, rule),
        "yo_yo": yo_yo(inputs, rule),
        "contract_pressure": contract_pressure(inputs, rule, day),
    }
    return {flag: detail for flag, detail in raised.items() if detail is not None}


def compute_flags(db: sessionmaker[Session], day: date, rule: FlagRule) -> dict[str, Any]:
    """Make `day`'s `player_flag` rows exactly the flags its player-teams raise, deleting the
    date's flags and writing them afresh; return the count."""
    with db.begin() as session:
        tiers = {
            (player_id, team_id): tier
            for player_id, team_id, tier in session.execute(
                select(RotationState.player_id, RotationState.team_id, RotationState.tier).where(
                    RotationState.game_date == day
                )
            )
        }
        keys = set(tiers)
        inputs = _inputs(session, day, keys, tiers, rule)
        rows = [
            {
                "player_id": player_id,
                "team_id": team_id,
                "game_date": day,
                "flag": flag,
                "detail": detail,
            }
            for (player_id, team_id) in sorted(keys)
            for flag, detail in flags(inputs[(player_id, team_id)], rule, day).items()
        ]
        removed = len(
            session.execute(
                delete(PlayerFlag)
                .where(PlayerFlag.game_date == day)
                .returning(PlayerFlag.player_id)
            ).all()
        )
        if rows:
            session.execute(insert(PlayerFlag), rows)
    return {"flags": len(rows), "removed": removed}


def _inputs(
    session: Session,
    day: date,
    keys: set[tuple[int, int]],
    tiers: dict[tuple[int, int], str],
    rule: FlagRule,
) -> dict[tuple[int, int], FlagInputs]:
    if not keys:
        return {}
    windows = _windows(session, day, keys)
    trust = _trust_shares(session, day, keys)
    changes = _tier_changes(session, day, keys, rule.yo_yo_games)
    guarantees = _guarantees(session, day, keys)
    two_way = _two_way_active(session, day, keys)
    found: dict[tuple[int, int], FlagInputs] = {}
    for key in keys:
        window = windows.get(key, {})
        found[key] = FlagInputs(
            tier=tiers[key],
            share_5=window.get("share_5"),
            share_20=window.get("share_20"),
            usage_10=window.get("usage_10"),
            usage_delta=window.get("usage_delta"),
            ts_10=window.get("ts_10"),
            ts_delta=window.get("ts_delta"),
            net_10=window.get("net_10"),
            on_off_10=window.get("on_off_10"),
            possessions_10=window.get("possessions_10"),
            trust_shares=trust.get(key, ()),
            changes=changes.get(key, ()),
            guarantee=guarantees.get(key),
            two_way_active=two_way.get(key),
        )
    return found


def _windows(
    session: Session, day: date, keys: set[tuple[int, int]]
) -> dict[tuple[int, int], dict[str, Any]]:
    """Each player-team's 5-, 10- and 20-game role window numbers on `day`, and his 10-game
    production window's usage and true shooting against his season's, from `player_window_stats`
    as last refreshed."""
    found = session.execute(
        text(
            'SELECT player_id, team_id, "window", minutes_share, on_court_net, on_off_net, '
            "on_court_poss, usg_pct, usg_pct_delta_season, ts_pct, ts_pct_delta_season "
            "FROM player_window_stats WHERE game_date = :day AND \"window\" IN ('5', '10', '20') "
            "AND player_id = ANY(:players)"
        ),
        {"day": day, "players": sorted({player_id for player_id, _ in keys})},
    ).all()
    production: dict[int, Any] = {}
    role: dict[tuple[int, int], dict[str, Any]] = defaultdict(dict)
    for row in found:
        if row.team_id is None:
            if row.window == "10":
                production[row.player_id] = row
            continue
        numbers = role[(row.player_id, row.team_id)]
        if row.window == "5":
            numbers["share_5"] = row.minutes_share
        elif row.window == "20":
            numbers["share_20"] = row.minutes_share
        else:
            numbers["net_10"] = row.on_court_net
            numbers["on_off_10"] = row.on_off_net
            # A sum of possessions, which Postgres returns as numeric.
            poss = row.on_court_poss
            numbers["possessions_10"] = None if poss is None else int(poss)
    windows: dict[tuple[int, int], dict[str, Any]] = {}
    for player_id, team_id in keys:
        numbers = dict(role.get((player_id, team_id), {}))
        ten = production.get(player_id)
        if ten is not None:
            numbers["usage_10"] = ten.usg_pct
            numbers["usage_delta"] = ten.usg_pct_delta_season
            numbers["ts_10"] = ten.ts_pct
            numbers["ts_delta"] = ten.ts_pct_delta_season
        windows[(player_id, team_id)] = numbers
    return windows


def _trust_shares(
    session: Session, day: date, keys: set[tuple[int, int]]
) -> dict[tuple[int, int], tuple[float | None, ...]]:
    """Each player-team's 5-game minutes shares at `day` and 5 and 10 of its game dates before,
    the oldest first, when his current spell with the team this season spans at least 15 of them.

    A role row is one per team game date while he is on the roster, a spell's dates consecutive,
    so the season role window's game count says whether the 15 latest rows are all this spell's."""
    found = session.execute(
        text(
            "SELECT w.player_id, w.team_id, w.rn, w.minutes_share FROM ("
            "  SELECT player_id, team_id, minutes_share, row_number() OVER ("
            "    PARTITION BY player_id, team_id ORDER BY game_date DESC"
            "  ) AS rn"
            "  FROM player_window_stats"
            "  WHERE team_id IS NOT NULL AND \"window\" = '5' AND game_date <= :day"
            "    AND player_id = ANY(:players)"
            ") w"
            " JOIN player_window_stats s ON s.player_id = w.player_id AND s.team_id = w.team_id"
            "  AND s.game_date = :day AND s.\"window\" = 'season' AND s.games_in_window >= 15"
            " WHERE w.rn IN (1, 6, 11)"
        ),
        {"day": day, "players": sorted({player_id for player_id, _ in keys})},
    )
    by_rank: dict[tuple[int, int], dict[int, float | None]] = defaultdict(dict)
    for player_id, team_id, rn, share in found:
        by_rank[(player_id, team_id)][rn] = share
    return {
        key: (ranks[11], ranks[6], ranks[1])
        for key, ranks in by_rank.items()
        if key in keys and len(ranks) == 3
    }


def _team_dates(session: Session, day: date, team_ids: set[int], games: int) -> dict[int, date]:
    """Each team's earliest date among its last `games` final game dates through `day` this
    season."""
    season = session.scalars(select(Game.season).where(Game.game_date == day).limit(1)).first()
    if season is None:
        return {}
    found = session.execute(
        select(Game.game_date, Game.home_team_id, Game.away_team_id)
        .where(Game.season == season, Game.game_date <= day, Game.status == "final")
        .where(or_(Game.home_team_id.in_(team_ids), Game.away_team_id.in_(team_ids)))
        .order_by(Game.game_date.desc())
    )
    dates: dict[int, list[date]] = defaultdict(list)
    for game_date, home, away in found:
        for team_id in (home, away):
            # A team plays once a date.
            if team_id in team_ids and len(dates[team_id]) < games:
                dates[team_id].append(game_date)
    return {team_id: listed[-1] for team_id, listed in dates.items() if listed}


def _tier_changes(
    session: Session, day: date, keys: set[tuple[int, int]], games: int
) -> dict[tuple[int, int], tuple[TierChange, ...]]:
    """Each player-team's tier changes over the team's last `games` final game dates through `day`
    this season, the oldest first: its rotation events but arrivals, on dates whose
    `rotation_state` row is not short."""
    since = _team_dates(session, day, {team_id for _, team_id in keys}, games)
    if not since:
        return {}
    found = session.execute(
        select(
            RotationEvent.player_id,
            RotationEvent.team_id,
            RotationEvent.game_date,
            RotationEvent.from_tier,
            RotationEvent.to_tier,
        )
        .join(
            RotationState,
            (RotationState.player_id == RotationEvent.player_id)
            & (RotationState.team_id == RotationEvent.team_id)
            & (RotationState.game_date == RotationEvent.game_date),
        )
        .where(tuple_(RotationEvent.player_id, RotationEvent.team_id).in_(list(keys)))
        .where(RotationEvent.game_date >= min(since.values()), RotationEvent.game_date <= day)
        .where(RotationEvent.from_tier.is_not(None), RotationState.is_short.is_(False))
        .order_by(RotationEvent.game_date)
    )
    changes: dict[tuple[int, int], list[TierChange]] = defaultdict(list)
    for player_id, team_id, game_date, from_tier, to_tier in found:
        start = since.get(team_id)
        if start is not None and game_date >= start:
            # Arrivals, the events with no `from_tier`, are left out above.
            change = TierChange(game_date, cast(str, from_tier), to_tier)
            changes[(player_id, team_id)].append(change)
    return {key: tuple(listed) for key, listed in changes.items()}


def _guarantees(
    session: Session, day: date, keys: set[tuple[int, int]]
) -> dict[tuple[int, int], Guarantee]:
    """Each player-team's next guarantee on or after `day` in the latest weekly snapshot on or
    before it, the earliest where the snapshot holds more than one; a player the snapshot does not
    list is fully guaranteed, whatever earlier ones said."""
    as_of = session.scalar(select(func.max(Contract.as_of_date)).where(Contract.as_of_date <= day))
    if as_of is None:
        return {}
    found = session.execute(
        select(
            Contract.player_id,
            Contract.team_id,
            Contract.guarantee_date,
            Contract.guaranteed,
            Contract.guaranteed_after,
        )
        .where(Contract.as_of_date == as_of, Contract.guarantee_date >= day)
        .where(tuple_(Contract.player_id, Contract.team_id).in_(list(keys)))
        .order_by(Contract.guarantee_date)
    )
    guarantees: dict[tuple[int, int], Guarantee] = {}
    for player_id, team_id, guarantee_date, guaranteed, guaranteed_after in found:
        guarantees.setdefault(
            (player_id, team_id), Guarantee(guarantee_date, guaranteed, guaranteed_after)
        )
    return guarantees


def _two_way_active(
    session: Session, day: date, keys: set[tuple[int, int]]
) -> dict[tuple[int, int], int]:
    """Each two-way player-team's regular-season games active through `day` this season, since the
    two-way span covering `day` started."""
    spans = session.execute(
        select(RosterMembership.player_id, RosterMembership.team_id, RosterMembership.start_date)
        .where(RosterMembership.contract_type == "two_way")
        .where(tuple_(RosterMembership.player_id, RosterMembership.team_id).in_(list(keys)))
        .where(RosterMembership.start_date <= day)
        .where(or_(RosterMembership.end_date.is_(None), RosterMembership.end_date >= day))
    ).all()
    if not spans:
        return {}
    season = session.scalars(select(Game.season).where(Game.game_date == day).limit(1)).first()
    started = {(player_id, team_id): start for player_id, team_id, start in spans}
    found = session.execute(
        select(PlayerGame.player_id, PlayerGame.team_id, Game.game_date)
        .join(Game, Game.game_id == PlayerGame.game_id)
        .where(tuple_(PlayerGame.player_id, PlayerGame.team_id).in_(list(started)))
        .where(Game.season == season, Game.phase.in_(REGULAR_SEASON), Game.status == "final")
        .where(Game.game_date >= min(started.values()), Game.game_date <= day)
        .where(
            or_(
                PlayerGame.dnp_reason.is_(None),
                PlayerGame.dnp_reason.in_(ACTIVE_DNP_REASONS),
            )
        )
    )
    active = dict.fromkeys(started, 0)
    for player_id, team_id, game_date in found:
        if game_date >= started[(player_id, team_id)]:
            active[(player_id, team_id)] += 1
    return active
