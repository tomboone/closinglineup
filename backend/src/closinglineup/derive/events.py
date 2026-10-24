"""Rotation events: each change in a player's rotation tier on a team between its game dates, and
each arrival on a team, with the signals that most explain it (spec §6.4, §7.6, ADR-0003).

`compute_events` writes a date's events from `rotation_state`, so it runs after
`derive.rotation.compute_rotation_state` has written the date and the team's previous game date. A
player-team with a row on the date has an event when its tier differs from his row's on the team's
previous final game date, or when he has no row there: an arrival, `from_tier` and
`prev_game_date` NULL. A player who leaves a team has no row on the date, and so no event.

`signals` ranks the candidates of spec §7.6 and keeps the top `SignalRule.top`, each as
`{name, value, label}`. A numeric candidate's magnitude is its value over the player's own
standard deviation, over his 20-game role window, of the per-game quantity it measures, so a change
counts for more in a player whose numbers rarely move:

- `minutes_share`: the 5-game less the 20-game minutes share, over the sd of his per-game share in
  the window's team games, a game he did not play a share of 0;
- `minutes_volatility`: the 5-game less the 20-game minutes volatility, over the 20-game's, the sd
  of his minutes in the games he played;
- `starter_rate`: the 5-game less the 20-game starter rate, over the sd of whether he started each
  game he played;
- `on_court_net`: the 10-game on-court net rating less the 10 games' before it, over the sd of his
  per-game on-court net rating, garbage time excluded as in `player_window_stats`;
- `usage`: the 10-game usage rate less the 10 games' before it, over the sd of his per-game usage
  in the games he played; both over his production window, whatever his team, not his role window.

The 5- and 10-game windows and the 10 games before the latter all lie inside their 20-game window,
so a nonzero change always has a nonzero sd. A candidate whose value or sd is missing, a short
window's, or whose value is zero, is not a signal. The same containment caps a numeric magnitude:
a whole step change in a stat that never otherwise moves is about 1.7 sd for a 5- vs 20-game
change and 1.9 for a 10-game one. So the two categorical candidates' fixed magnitudes sit within
that range, an injury return outranking a modest minutes change but not a cliff, when present:

- `injury`: his availability changed, to or from an injury report status, on one of the team's
  game dates in the last `SignalRule.lookback_days`, the date included; the latest change, as
  `from → to`;
- `transaction`: a transaction moving him to or from the team in the last
  `SignalRule.lookback_days`, the date included; the latest one's type.

Ties keep the order above.
"""

import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

from sqlalchemy import delete, func, insert, or_, select, text, tuple_
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import (
    Game,
    PlayerGame,
    RotationEvent,
    RotationState,
    Stint,
    Transaction,
)

# Availabilities only an injury report gives: a change to or from one is an injury status change.
INJURY_STATUSES = frozenset({"out", "doubtful", "questionable", "probable"})
# A regulation game's length in seconds, and an overtime's.
REGULATION = 2880
OVERTIME = 300
TRANSACTION_VERBS = {
    "sign": "signed",
    "waive": "waived",
    "trade": "traded",
    "two_way_sign": "signed to a two-way",
    "two_way_convert": "converted from a two-way",
    "assign": "assigned to the G League",
    "recall": "recalled from the G League",
    "option_exercise": "option exercised",
    "option_decline": "option declined",
    "other": "transaction",
}


@dataclass(frozen=True, slots=True)
class SignalRule:
    """How many signals an event keeps, how far back the categorical ones look, and their fixed
    magnitudes, on the numeric candidates' scale of standard deviations."""

    top: int = 3
    lookback_days: int = 7
    injury_magnitude: float = 1.5
    transaction_magnitude: float = 1.0


@dataclass(frozen=True, slots=True)
class InjuryChange:
    """His latest availability change: from, to, and how many days before the event's date."""

    before: str
    after: str
    days_ago: int


@dataclass(frozen=True, slots=True)
class TransactionNote:
    """His latest transaction with the team: its type and how many days before the event's date."""

    type: str
    days_ago: int


@dataclass(frozen=True, slots=True)
class SignalInputs:
    """Each numeric candidate's value and the standard deviation it is measured against, and the
    categorical candidates when present; None where there is nothing to read."""

    share_delta: float | None = None
    share_sd: float | None = None
    volatility_delta: float | None = None
    minutes_sd: float | None = None
    starter_delta: float | None = None
    starter_sd: float | None = None
    net_delta: float | None = None
    net_sd: float | None = None
    usage_delta: float | None = None
    usage_sd: float | None = None
    injury: InjuryChange | None = None
    transaction: TransactionNote | None = None


def _pts(fraction: float) -> str:
    """A fraction's size in percentage points: 0.09 is "9", 0.086 is "8.6"."""
    return f"{abs(fraction) * 100:.1f}".removesuffix(".0")


def _ago(days: int) -> str:
    if days == 0:
        return "today"
    return "1 day ago" if days == 1 else f"{days} days ago"


def _injury_label(change: InjuryChange) -> str:
    when = _ago(change.days_ago)
    if change.after in INJURY_STATUSES and change.before not in INJURY_STATUSES:
        return f"listed {change.after} {when}"
    if change.before in INJURY_STATUSES and change.after not in INJURY_STATUSES:
        return f"back from {change.before} {when}"
    return f"status {change.before} → {change.after} {when}"


def _label(name: str, value: float) -> str:
    """A numeric signal's plain-English label."""
    way = "up" if value > 0 else "down"
    match name:
        case "minutes_share":
            return f"minutes share {way} {_pts(value)} pts vs 20-game"
        case "minutes_volatility":
            return f"minutes volatility {way} {abs(value):.1f} min vs 20-game"
        case "starter_rate":
            return f"starter rate {way} {_pts(value)} pts vs 20-game"
        case "on_court_net":
            return f"on-court net {way} {abs(value):.1f} vs prior 10 games"
        case _:
            return f"usage {way} {_pts(value)} pts vs prior 10 games"


def signals(inputs: SignalInputs, rule: SignalRule) -> list[dict[str, Any]]:
    """The top `rule.top` signals of spec §7.6 in `inputs`, the largest magnitude first."""
    numeric = [
        ("minutes_share", inputs.share_delta, inputs.share_sd),
        ("minutes_volatility", inputs.volatility_delta, inputs.minutes_sd),
        ("starter_rate", inputs.starter_delta, inputs.starter_sd),
        ("on_court_net", inputs.net_delta, inputs.net_sd),
        ("usage", inputs.usage_delta, inputs.usage_sd),
    ]
    candidates: list[tuple[float, dict[str, Any]]] = [
        (abs(value) / sd, {"name": name, "value": round(value, 4), "label": _label(name, value)})
        for name, value, sd in numeric
        if value and sd
    ]
    if inputs.injury is not None:
        change = inputs.injury
        candidates.append(
            (
                rule.injury_magnitude,
                {
                    "name": "injury",
                    "value": f"{change.before} → {change.after}",
                    "label": _injury_label(change),
                },
            )
        )
    if inputs.transaction is not None:
        note = inputs.transaction
        verb = TRANSACTION_VERBS.get(note.type, "transaction")
        candidates.append(
            (
                rule.transaction_magnitude,
                {
                    "name": "transaction",
                    "value": note.type,
                    "label": f"{verb} {_ago(note.days_ago)}",
                },
            )
        )
    # `sorted` is stable, so ties keep the candidates' order.
    ranked = sorted(candidates, key=lambda candidate: -candidate[0])
    return [signal for _, signal in ranked[: rule.top]]


def compute_events(db: sessionmaker[Session], day: date, rule: SignalRule) -> dict[str, Any]:
    """Make `day`'s `rotation_event` rows exactly the tier changes and arrivals in its
    `rotation_state`, deleting the date's events and writing them afresh; return the count."""
    with db.begin() as session:
        today = session.execute(
            select(RotationState.player_id, RotationState.team_id, RotationState.tier).where(
                RotationState.game_date == day
            )
        ).all()
        prev_dates = _previous_game_dates(session, day, {team_id for _, team_id, _ in today})
        before = _tiers(session, prev_dates)
        changed: list[tuple[int, int, str, date | None, str | None]] = []
        for player_id, team_id, to_tier in sorted(today):
            prev_date = prev_dates.get(team_id)
            from_tier = before.get((player_id, team_id))
            if from_tier == to_tier:
                continue
            changed.append(
                (player_id, team_id, to_tier, prev_date if from_tier else None, from_tier)
            )
        inputs = _signal_inputs(
            session, day, {(player_id, team_id) for player_id, team_id, *_ in changed}, rule
        )
        rows = [
            {
                "player_id": player_id,
                "team_id": team_id,
                "game_date": day,
                "prev_game_date": prev_date,
                "from_tier": from_tier,
                "to_tier": to_tier,
                "signals": signals(inputs[(player_id, team_id)], rule),
            }
            for player_id, team_id, to_tier, prev_date, from_tier in changed
        ]
        removed = len(
            session.execute(
                delete(RotationEvent)
                .where(RotationEvent.game_date == day)
                .returning(RotationEvent.id)
            ).all()
        )
        if rows:
            session.execute(insert(RotationEvent), rows)
    return {"events": len(rows), "removed": removed}


def _previous_game_dates(session: Session, day: date, team_ids: set[int]) -> dict[int, date]:
    """Each team's last final game date before `day`, in any season."""
    found: dict[int, date] = {}
    for side in (Game.home_team_id, Game.away_team_id):
        for team_id, game_date in session.execute(
            select(side, func.max(Game.game_date))
            .where(side.in_(team_ids), Game.game_date < day, Game.status == "final")
            .group_by(side)
        ):
            found[team_id] = max(found.get(team_id, game_date), game_date)
    return found


def _tiers(session: Session, prev_dates: dict[int, date]) -> dict[tuple[int, int], str]:
    """Each player-team's tier on its team's previous game date."""
    if not prev_dates:
        return {}
    found = session.execute(
        select(RotationState.player_id, RotationState.team_id, RotationState.tier).where(
            tuple_(RotationState.team_id, RotationState.game_date).in_(list(prev_dates.items()))
        )
    )
    return {(player_id, team_id): tier for player_id, team_id, tier in found}


@dataclass(frozen=True, slots=True)
class _Windows:
    """His role windows' and 10-game production window's numbers on the date."""

    games_20: int = 0
    share_5: float | None = None
    share_20: float | None = None
    volatility_5: float | None = None
    volatility_20: float | None = None
    starter_rate_5: float | None = None
    starter_rate_20: float | None = None
    net_delta_10: float | None = None
    usage_delta_10: float | None = None


def _delta(later: float | None, earlier: float | None) -> float | None:
    return None if later is None or earlier is None else later - earlier


def _sd(values: Sequence[float]) -> float | None:
    """The sample standard deviation, as `player_window_stats`'s minutes volatility is; None for
    fewer than two values."""
    return statistics.stdev(values) if len(values) >= 2 else None


def _signal_inputs(
    session: Session, day: date, keys: set[tuple[int, int]], rule: SignalRule
) -> dict[tuple[int, int], SignalInputs]:
    """Each event's `SignalInputs`, from `player_window_stats` as last refreshed, the box scores and
    stints of his 20-game windows' games, `rotation_state` and `transaction`."""
    if not keys:
        return {}
    windows = _windows(session, day, keys)
    team_games = _team_games(session, day, {team_id for _, team_id in keys})
    games = _per_game(session, team_games, keys, windows)
    usages = _usages(session, day, {player_id for player_id, _ in keys})
    # The last `lookback_days` days, the date the last of them.
    since = day - timedelta(days=rule.lookback_days - 1)
    injuries = _injury_changes(session, day, since, keys)
    transactions = _transactions(session, day, since, keys)
    found: dict[tuple[int, int], SignalInputs] = {}
    for key in keys:
        window = windows.get(key, _Windows())
        per_game = games.get(key, _PerGame())
        found[key] = SignalInputs(
            share_delta=_delta(window.share_5, window.share_20),
            share_sd=_sd(per_game.shares),
            volatility_delta=_delta(window.volatility_5, window.volatility_20),
            minutes_sd=window.volatility_20,
            starter_delta=_delta(window.starter_rate_5, window.starter_rate_20),
            starter_sd=_sd(per_game.started),
            net_delta=window.net_delta_10,
            net_sd=_sd(per_game.nets),
            usage_delta=window.usage_delta_10,
            usage_sd=_sd(usages.get(key[0], [])),
            injury=injuries.get(key),
            transaction=transactions.get(key),
        )
    return found


def _windows(
    session: Session, day: date, keys: set[tuple[int, int]]
) -> dict[tuple[int, int], _Windows]:
    """Each player-team's 5-, 10- and 20-game role windows on `day`, and his 10-game production
    window's usage change."""
    found = session.execute(
        text(
            'SELECT player_id, team_id, "window", games_in_window, minutes_share, '
            "minutes_volatility, starter_rate, on_court_net_delta_prior, usg_pct_delta_prior "
            "FROM player_window_stats WHERE game_date = :day AND \"window\" IN ('5', '10', '20') "
            "AND player_id = ANY(:players)"
        ),
        {"day": day, "players": sorted({player_id for player_id, _ in keys})},
    ).all()
    role: dict[tuple[int, int, str], Any] = {}
    usage: dict[int, float | None] = {}
    for row in found:
        if row.team_id is None:
            if row.window == "10":
                usage[row.player_id] = row.usg_pct_delta_prior
        else:
            role[(row.player_id, row.team_id, row.window)] = row
    windows: dict[tuple[int, int], _Windows] = {}
    for player_id, team_id in keys:
        five = role.get((player_id, team_id, "5"))
        ten = role.get((player_id, team_id, "10"))
        twenty = role.get((player_id, team_id, "20"))
        if five is None or ten is None or twenty is None:
            continue
        windows[(player_id, team_id)] = _Windows(
            games_20=twenty.games_in_window,
            share_5=five.minutes_share,
            share_20=twenty.minutes_share,
            volatility_5=five.minutes_volatility,
            volatility_20=twenty.minutes_volatility,
            starter_rate_5=five.starter_rate,
            starter_rate_20=twenty.starter_rate,
            net_delta_10=ten.on_court_net_delta_prior,
            usage_delta_10=usage.get(player_id),
        )
    return windows


def _team_games(session: Session, day: date, team_ids: set[int]) -> dict[int, list[str]]:
    """Each team's last 20 final games through `day` in its season, newest first, one a date (the
    first by game ID, as `player_window_stats` counts them)."""
    season = session.scalars(select(Game.season).where(Game.game_date == day).limit(1)).first()
    if season is None:
        return {}
    found = session.execute(
        select(Game.game_id, Game.game_date, Game.home_team_id, Game.away_team_id)
        .where(Game.season == season, Game.game_date <= day, Game.status == "final")
        .where(or_(Game.home_team_id.in_(team_ids), Game.away_team_id.in_(team_ids)))
        .order_by(Game.game_date.desc(), Game.game_id)
    )
    dated: dict[int, dict[date, str]] = defaultdict(dict)
    for game_id, game_date, home, away in found:
        for team_id in (home, away):
            if team_id in team_ids:
                dated[team_id].setdefault(game_date, game_id)
    return {team_id: list(by_date.values())[:20] for team_id, by_date in dated.items()}


@dataclass(slots=True)
class _PerGame:
    """His per-game numbers over his 20-game role window: a minutes share for each team game, and
    for each game he played whether he started (1 or 0) and his on-court net rating where it is
    defined."""

    shares: list[float] = field(default_factory=list[float])
    started: list[float] = field(default_factory=list[float])
    nets: list[float] = field(default_factory=list[float])


def _available_seconds(seconds: float) -> int:
    """A team's available player-seconds in a game: five players over its length, the overtimes
    read off the players' seconds as `player_window_stats` reads them, rounding half up."""
    overtimes = math.floor(max(seconds / 5 - REGULATION, 0) / OVERTIME + 0.5)
    return 5 * (REGULATION + OVERTIME * overtimes)


def _per_game(
    session: Session,
    team_games: dict[int, list[str]],
    keys: set[tuple[int, int]],
    windows: dict[tuple[int, int], _Windows],
) -> dict[tuple[int, int], _PerGame]:
    """Each player-team's `_PerGame` over the team's games in his 20-game role window."""
    game_ids = sorted({game_id for ids in team_games.values() for game_id in ids})
    if not game_ids:
        return {}
    team_seconds: dict[tuple[str, int], int] = defaultdict(int)
    lines: dict[tuple[str, int, int], Any] = {}
    for line in session.execute(
        select(
            PlayerGame.game_id,
            PlayerGame.player_id,
            PlayerGame.team_id,
            PlayerGame.seconds,
            PlayerGame.is_starter,
        ).where(PlayerGame.game_id.in_(game_ids))
    ):
        team_seconds[(line.game_id, line.team_id)] += line.seconds
        if (line.player_id, line.team_id) in keys:
            lines[(line.game_id, line.player_id, line.team_id)] = line
    nets = _on_court_nets(session, game_ids, keys)
    found: dict[tuple[int, int], _PerGame] = {}
    for player_id, team_id in keys:
        window = windows.get((player_id, team_id))
        if window is None:
            continue
        per_game = found.setdefault((player_id, team_id), _PerGame())
        for game_id in team_games.get(team_id, [])[: window.games_20]:
            line = lines.get((game_id, player_id, team_id))
            seconds = line.seconds if line is not None else 0
            per_game.shares.append(seconds / _available_seconds(team_seconds[(game_id, team_id)]))
            if line is None or seconds <= 0:
                continue
            per_game.started.append(1.0 if line.is_starter else 0.0)
            net = nets.get((game_id, player_id, team_id))
            if net is not None:
                per_game.nets.append(net)
    return found


def _usages(session: Session, day: date, player_ids: set[int]) -> dict[int, list[float]]:
    """Each player's usage in each game he played of his 20-game production window on `day`, as
    `player_window_stats` computes it: his plays (attempts, a free throw attempt 0.44 of one, and
    turnovers) over his team's while he was on the floor."""
    found = session.execute(
        text(
            "WITH window_games AS ("
            "  SELECT d.player_id, d.game_date"
            "  FROM player_window_stats w"
            "  JOIN LATERAL ("
            "    SELECT player_id, game_date FROM player_window_stats"
            "    WHERE player_id = w.player_id AND team_id IS NULL AND \"window\" = '20'"
            "      AND game_date <= :day"
            "    ORDER BY game_date DESC LIMIT w.games_in_window"
            "  ) d ON true"
            "  WHERE w.game_date = :day AND w.team_id IS NULL AND w.\"window\" = '20'"
            "    AND w.player_id = ANY(:players)"
            "), team_box AS ("
            "  SELECT game_id, team_id, sum(seconds) AS seconds, sum(fga) AS fga,"
            "    sum(fta) AS fta, sum(tov) AS tov"
            "  FROM player_game GROUP BY game_id, team_id"
            ")"
            " SELECT pg.player_id, pg.seconds, pg.fga, pg.fta, pg.tov,"
            "   tb.seconds AS team_seconds, tb.fga AS team_fga, tb.fta AS team_fta,"
            "   tb.tov AS team_tov"
            " FROM window_games wg"
            " JOIN game g ON g.game_date = wg.game_date AND g.status = 'final'"
            " JOIN player_game pg ON pg.game_id = g.game_id AND pg.player_id = wg.player_id"
            " JOIN team_box tb ON tb.game_id = pg.game_id AND tb.team_id = pg.team_id"
            " WHERE pg.seconds > 0"
        ),
        {"day": day, "players": sorted(player_ids)},
    )
    usages: dict[int, list[float]] = defaultdict(list)
    for row in found:
        plays = row.team_fga + 0.44 * row.team_fta + row.team_tov
        if plays > 0:
            usages[row.player_id].append(
                (row.fga + 0.44 * row.fta + row.tov)
                * (row.team_seconds / 5)
                / (row.seconds * plays)
            )
    return usages


def _on_court_nets(
    session: Session, game_ids: list[str], keys: set[tuple[int, int]]
) -> dict[tuple[str, int, int], float]:
    """Each player-team's on-court net rating in each game, garbage time excluded, as
    `player_window_stats`'s: offense over the team's possessions with him on the floor, defense
    over the opponent's in the same stints; none where either count is zero."""
    stints = session.execute(
        select(
            Stint.game_id,
            Stint.period,
            Stint.start_sec,
            Stint.team_id,
            Stint.lineup_key,
            Stint.possessions,
            Stint.pts_for,
            Stint.pts_against,
            Stint.is_garbage_time,
        ).where(Stint.game_id.in_(game_ids))
    ).all()
    # Each stint's possessions by where it starts and whose: the other team's are the defense's.
    starting: dict[tuple[str, int, int], dict[int, int]] = defaultdict(dict)
    for s in stints:
        starting[(s.game_id, s.period, s.start_sec)][s.team_id] = s.possessions
    # (poss, opp poss, pts for, pts against) per (game, player, team).
    sums: dict[tuple[str, int, int], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for s in stints:
        if s.is_garbage_time:
            continue
        sides = starting[(s.game_id, s.period, s.start_sec)]
        opp = sum(count for team_id, count in sides.items() if team_id != s.team_id)
        for member in s.lineup_key.split("-"):
            key = (s.game_id, int(member), s.team_id)
            if (key[1], key[2]) not in keys:
                continue
            total = sums[key]
            total[0] += s.possessions
            total[1] += opp
            total[2] += s.pts_for
            total[3] += s.pts_against
    return {
        key: 100.0 * pts_for / poss - 100.0 * pts_against / opp_poss
        for key, (poss, opp_poss, pts_for, pts_against) in sums.items()
        if poss > 0 and opp_poss > 0
    }


def _injury_changes(
    session: Session, day: date, since: date, keys: set[tuple[int, int]]
) -> dict[tuple[int, int], InjuryChange]:
    """Each player-team's latest availability change to or from an injury report status on one of
    its game dates from `since` through `day`, read off `rotation_state`, his last row before
    `since` included as where the first change starts from."""
    player_team = tuple_(RotationState.player_id, RotationState.team_id)
    # Reading from the earliest of the player-teams' last rows before `since` reads each one's.
    before = session.execute(
        select(func.max(RotationState.game_date))
        .where(player_team.in_(list(keys)), RotationState.game_date < since)
        .group_by(RotationState.player_id, RotationState.team_id)
    ).scalars()
    earliest = min([since, *before])
    found = session.execute(
        select(
            RotationState.player_id,
            RotationState.team_id,
            RotationState.game_date,
            RotationState.availability,
        )
        .where(player_team.in_(list(keys)))
        .where(RotationState.game_date >= earliest, RotationState.game_date <= day)
        .order_by(RotationState.game_date)
    )
    history: dict[tuple[int, int], list[tuple[date, str]]] = defaultdict(list)
    for player_id, team_id, game_date, status in found:
        history[(player_id, team_id)].append((game_date, status))
    changes: dict[tuple[int, int], InjuryChange] = {}
    for key, rows in history.items():
        for (_, before), (game_date, after) in pairwise(rows):
            changed = before != after and (before in INJURY_STATUSES or after in INJURY_STATUSES)
            if changed and game_date >= since:
                changes[key] = InjuryChange(before, after, (day - game_date).days)
    return changes


def _transactions(
    session: Session, day: date, since: date, keys: set[tuple[int, int]]
) -> dict[tuple[int, int], TransactionNote]:
    """Each player-team's latest transaction moving him to or from the team from `since` through
    `day`."""
    found = session.execute(
        select(
            Transaction.player_id,
            Transaction.from_team_id,
            Transaction.to_team_id,
            Transaction.date,
            Transaction.type,
        )
        .where(Transaction.player_id.in_({player_id for player_id, _ in keys}))
        .where(Transaction.date >= since, Transaction.date <= day)
        .order_by(Transaction.date, Transaction.id)
    )
    notes: dict[tuple[int, int], TransactionNote] = {}
    for player_id, from_team_id, to_team_id, when, kind in found:
        for team_id in (from_team_id, to_team_id):
            if team_id is not None and (player_id, team_id) in keys:
                notes[(player_id, team_id)] = TransactionNote(kind, (day - when).days)
    return notes
