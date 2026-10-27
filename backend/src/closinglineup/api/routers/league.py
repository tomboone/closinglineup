"""`GET /api/league`: the league morning page (spec §8, §14), everything read off the date alone, so
a date no team played has empty lists:

- the **games** are the date's final games, by tip-off, each side with its two highest Game Scores
  (`derive.value.game_score`) among the players who played, ties by player (v2 spec §15.3);
- the **rotation events** are the date's `rotation_event` rows, by team, then by the tier moved to
  and player, each with the signals `derive.events` ranked for it;
- the **quiet signals** are the Starter and Core players, by the date's `rotation_state`, with no
  rotation event on it, who sat by coach's decision in their team's game on it or played under
  `QUIET_RATIO` of their minutes per game over the 10-game role window on the team's previous final
  game date this season, so the date's game is not in the average; a DNP for any other reason is
  neither: a DNP first, then the largest shortfall (v2 spec §5.4);
- the **minutes movers** are `player_window_stats`' 10-game role rows on the date, the 15 whose
  minutes share rose most over the 10 games before and the 15 whose fell most, a row whose share
  did not move being neither;
- the **flags** are those raised on the date, by flag in spec §7.7's order, then team and player,
  a yo-yo with his tier on each of the team's last 20 final game dates this season through it;
- the **injury changes** are, for each team, how its newest injury report by the end of the date
  differs from its newest by the end of the league's previous game date, each read as the team
  page reads one: a
  player newly Out or Doubtful, or back to Available or off the report from either. A G League
  line is an assignment, not an injury (spec §7.5), so it counts as no line;
- the **transactions** are the date's and the day after's, the morning the feed is read on (v2
  spec §3);
- the **impact movers** are the 10 whose impact rose most and the 10 whose fell most over the 10
  team games before the date, as `player_window_stats`' 10-game production rows carry it: each
  player's first of EPM, DARKO and local BPM with a value on both dates, named as its `source`.

Every player row carries his `impact` on the date, the first of DARKO and local BPM he has a
snapshot for, `player_impacts`' one query over them all (v2 spec §7).
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Any, NamedTuple, cast

from fastapi import APIRouter
from sqlalchemy import func, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from closinglineup.api.deps import DateDep, SessionDep
from closinglineup.api.routers.teams import (
    IMPACT_PREFERENCE,
    player_impacts,
    player_names,
    player_ref,
)
from closinglineup.api.schemas import (
    Flag,
    GameScoreLine,
    Impact,
    InjuryChange,
    LastNightGame,
    LastNightTeam,
    LeaguePage,
    Mover,
    QuietSignal,
    RaisedFlag,
    RotationEventSummary,
    Signal,
    TeamRotationEvents,
    Tier,
    TierPoint,
    TransactionSummary,
)
from closinglineup.api.schemas import (
    InjuryStatus as InjuryStatusName,
)
from closinglineup.db.models import (
    PLAYER_FLAGS,
    ROTATION_TIERS,
    Game,
    InjuryStatus,
    PlayerFlag,
    PlayerGame,
    RotationEvent,
    RotationState,
    Transaction,
)
from closinglineup.derive.availability import COACHS_DECISION, NOT_INJURIES
from closinglineup.derive.flags import FlagRule
from closinglineup.derive.value import game_score
from closinglineup.injuries import PDF_SOURCE
from closinglineup.season import season_bounds, season_label
from closinglineup.sources.injury_pdf import EASTERN
from closinglineup.teams import by_nba_team_id

router = APIRouter()

TOP_MINUTES_MOVERS = 15
# A Starter or Core player below this share of his 10-game minutes per game is a quiet signal.
QUIET_RATIO = 0.6
QUIET_TIERS = ("starter", "core")
TOP_IMPACT_MOVERS = 10
# Each side's highest Game Scores shown under a final game.
TOP_GAME_SCORES = 2
# The window the movers' deltas are over, in team games.
MOVER_GAMES = 10
# A yo-yo's tier strip spans the games its rule counts over.
STRIP_GAMES = FlagRule().yo_yo_games
# A player is out on a report at these statuses; back from them at none, or Available.
OUT = frozenset({"Out", "Doubtful"})
_TIER_RANK = {tier: rank for rank, tier in enumerate(ROTATION_TIERS)}
_FLAG_RANK = {flag: rank for rank, flag in enumerate(PLAYER_FLAGS)}


@router.get("/api/league")
def league(session: SessionDep, day: DateDep) -> LeaguePage:
    games = _games(session, day)
    events = _events(session, day)
    quiet = _quiet_signals(session, day)
    teams = _teams(session, day)
    minutes_risers, minutes_fallers = _minutes_movers(session, day)
    impact_risers, impact_fallers = _impact_movers(session, day, teams)
    flags = _flags(session, day)
    strips = _tier_strips(
        session,
        day,
        {(flag.player_id, flag.team_id) for flag in flags if flag.flag == "yo_yo"},
    )
    injuries = _injury_changes(session, day)
    transactions = _transactions(session, day)
    players = {
        *(event.player_id for event in events),
        *(signal.player_id for signal in quiet),
        *(mover.player_id for mover in (*minutes_risers, *minutes_fallers)),
        *(mover.player_id for mover in (*impact_risers, *impact_fallers)),
        *(flag.player_id for flag in flags),
        *(change.player_id for change in injuries),
        *(note.player_id for note in transactions),
    }
    names = player_names(
        session,
        players | {line.player_id for game in games for line in (*game.home, *game.away)},
    )
    impacts = player_impacts(session, players, day)
    by_team: dict[str, list[RotationEventSummary]] = defaultdict(list)
    for event in events:
        by_team[by_nba_team_id(event.team_id).tricode].append(
            RotationEventSummary(
                player=player_ref(event.player_id, names),
                game_date=event.game_date,
                from_tier=cast(Tier | None, event.from_tier),
                to_tier=cast(Tier, event.to_tier),
                signals=[Signal.model_validate(signal) for signal in event.signals],
                impact=impacts.get(event.player_id),
            )
        )
    return LeaguePage(
        date=day,
        games=[game.model(names) for game in games],
        rotation_events=[
            TeamRotationEvents(tricode=tricode, events=by_team[tricode])
            for tricode in sorted(by_team)
        ],
        quiet_signals=[signal.model(names, impacts) for signal in quiet],
        minutes_risers=[mover.model(names, impacts) for mover in minutes_risers],
        minutes_fallers=[mover.model(names, impacts) for mover in minutes_fallers],
        flags=[
            RaisedFlag(
                player=player_ref(flag.player_id, names),
                tricode=by_nba_team_id(flag.team_id).tricode,
                game_date=flag.game_date,
                flag=cast(Flag, flag.flag),
                detail=flag.detail,
                impact=impacts.get(flag.player_id),
                tiers=strips.get((flag.player_id, flag.team_id)) if flag.flag == "yo_yo" else None,
            )
            for flag in flags
        ],
        injury_changes=[change.model(names) for change in injuries],
        transactions=[
            TransactionSummary(
                player=player_ref(note.player_id, names),
                date=note.date,
                type=note.type,
                from_tricode=_tricode(note.from_team_id),
                to_tricode=_tricode(note.to_team_id),
                detail=note.detail,
                impact=impacts.get(note.player_id),
            )
            for note in transactions
        ],
        impact_risers=[mover.model(names, impacts) for mover in impact_risers],
        impact_fallers=[mover.model(names, impacts) for mover in impact_fallers],
    )


def _tricode(team_id: int | None) -> str | None:
    return None if team_id is None else by_nba_team_id(team_id).tricode


# Last night's games.


class _FinalGame(NamedTuple):
    game: Game
    # Each side's top Game Score lines, the highest first.
    home: list[PlayerGame]
    away: list[PlayerGame]

    def model(self, names: dict[int, str]) -> LastNightGame:
        game = self.game
        margin = None
        if game.home_score is not None and game.away_score is not None:
            margin = game.home_score - game.away_score
        return LastNightGame(
            game_id=game.game_id,
            home=_side(game.home_team_id, game.home_score, self.home, names),
            away=_side(game.away_team_id, game.away_score, self.away, names),
            margin=margin,
        )


def _side(
    team_id: int, score: int | None, lines: list[PlayerGame], names: dict[int, str]
) -> LastNightTeam:
    return LastNightTeam(
        tricode=by_nba_team_id(team_id).tricode,
        score=score,
        top=[
            GameScoreLine(
                player=player_ref(line.player_id, names),
                game_score=game_score(line),
                pts=line.pts,
                reb=line.oreb + line.dreb,
                ast=line.ast,
                seconds=line.seconds,
            )
            for line in lines
        ],
    )


def _games(session: Session, day: date) -> list[_FinalGame]:
    """The date's final games by tip-off, then ID, each side's `TOP_GAME_SCORES` highest Game
    Scores among the players who played, ties by player."""
    games = session.scalars(
        select(Game)
        .where(Game.game_date == day, Game.status == "final")
        .order_by(Game.tip_time.asc().nulls_last(), Game.game_id)
    ).all()
    lines: dict[tuple[str, int], list[PlayerGame]] = defaultdict(list)
    for line in session.scalars(
        select(PlayerGame).where(
            PlayerGame.game_id.in_([game.game_id for game in games]),
            PlayerGame.dnp_reason.is_(None),
        )
    ):
        lines[line.game_id, line.team_id].append(line)

    def top(game: Game, team_id: int) -> list[PlayerGame]:
        ranked = sorted(
            lines[game.game_id, team_id], key=lambda line: (-game_score(line), line.player_id)
        )
        return ranked[:TOP_GAME_SCORES]

    return [
        _FinalGame(game, top(game, game.home_team_id), top(game, game.away_team_id))
        for game in games
    ]


def _events(session: Session, day: date) -> list[RotationEvent]:
    """The date's rotation events, by the tier moved to then player."""
    found = session.scalars(select(RotationEvent).where(RotationEvent.game_date == day))
    return sorted(found, key=lambda event: (_TIER_RANK[event.to_tier], event.player_id))


def _teams(session: Session, day: date) -> dict[int, int]:
    """Each player whose team played on the date, to that team: the one he has a box line for,
    should two of his spells' teams both play, as the view's production rows pick it."""
    teams = dict(
        session.execute(
            text(
                "SELECT player_id, team_id FROM player_window_stats "
                "WHERE \"window\" = '10' AND game_date = :day AND team_id IS NOT NULL"
            ),
            {"day": day},
        ).all()
    )
    teams.update(
        session.execute(
            select(PlayerGame.player_id, PlayerGame.team_id)
            .join(Game, Game.game_id == PlayerGame.game_id)
            .where(Game.game_date == day)
        ).all()
    )
    return teams


# Quiet signals.


class QuietRow(NamedTuple):
    player_id: int
    team_id: int
    tier: str
    seconds: int
    minutes_per_game: float
    dnp_reason: str | None

    def rank(self) -> tuple[bool, float, int]:
        """A DNP first, then the largest shortfall of his average, then by player."""
        shortfall = self.minutes_per_game * 60 - self.seconds
        return (self.dnp_reason is None, -shortfall, self.player_id)

    def model(self, names: dict[int, str], impacts: dict[int, Impact]) -> QuietSignal:
        return QuietSignal(
            player=player_ref(self.player_id, names),
            tricode=by_nba_team_id(self.team_id).tricode,
            tier=cast(Tier, self.tier),
            seconds=self.seconds,
            minutes_per_game_10=self.minutes_per_game,
            dnp_reason=cast(Any, self.dnp_reason),
            impact=impacts.get(self.player_id),
        )


def _quiet_signals(session: Session, day: date) -> list[QuietRow]:
    """The date's box lines of Starters and Core players with no rotation event on it, against
    their 10-game role rows on their team's previous final game date this season: a DNP-CD, or a
    game played under `QUIET_RATIO` of the row's minutes per game."""
    found = session.execute(
        text(
            "SELECT pg.player_id, pg.team_id, rs.tier, pg.seconds, "
            "v.minutes_per_game::float8, pg.dnp_reason "
            "FROM player_game pg JOIN game g ON g.game_id = pg.game_id "
            "JOIN rotation_state rs ON rs.player_id = pg.player_id "
            "AND rs.team_id = pg.team_id AND rs.game_date = g.game_date "
            "CROSS JOIN LATERAL (SELECT max(p.game_date) AS game_date FROM game p "
            "WHERE pg.team_id IN (p.home_team_id, p.away_team_id) AND p.season = g.season "
            "AND p.status = 'final' AND p.game_date < g.game_date) prev "
            "JOIN player_window_stats v ON v.player_id = pg.player_id AND v.team_id = pg.team_id "
            "AND v.\"window\" = '10' AND v.game_date = prev.game_date "
            "WHERE g.game_date = :day AND rs.tier = ANY(:tiers) "
            "AND v.minutes_per_game IS NOT NULL "
            "AND NOT EXISTS (SELECT FROM rotation_event e "
            "WHERE e.player_id = pg.player_id AND e.game_date = g.game_date) "
            "AND (pg.dnp_reason = :cd OR (pg.dnp_reason IS NULL "
            "AND pg.seconds < :ratio * v.minutes_per_game * 60))"
        ),
        {"day": day, "tiers": list(QUIET_TIERS), "cd": COACHS_DECISION, "ratio": QUIET_RATIO},
    ).all()
    return sorted((QuietRow(*row) for row in found), key=QuietRow.rank)


# Movers.


class MoverRow(NamedTuple):
    player_id: int
    team_id: int | None
    value: float | None
    delta: float
    source: str | None = None

    def model(self, names: dict[int, str], impacts: dict[int, Impact]) -> Mover:
        return Mover(
            player=player_ref(self.player_id, names),
            tricode=_tricode(self.team_id),
            value=self.value,
            delta=self.delta,
            source=cast(Any, self.source),
            impact=impacts.get(self.player_id),
        )


def top_movers(movers: Sequence[MoverRow], n: int) -> tuple[list[MoverRow], list[MoverRow]]:
    """The `n` that rose most, the largest rise first, and the `n` that fell most, the largest fall
    first; ties by player."""
    risers = sorted(
        (mover for mover in movers if mover.delta > 0),
        key=lambda mover: (-mover.delta, mover.player_id),
    )
    fallers = sorted(
        (mover for mover in movers if mover.delta < 0),
        key=lambda mover: (mover.delta, mover.player_id),
    )
    return risers[:n], fallers[:n]


def _minutes_movers(session: Session, day: date) -> tuple[list[MoverRow], list[MoverRow]]:
    """The 10-game role rows on the date by their minutes share's change over the 10 games
    before."""
    found = session.execute(
        text(
            "SELECT player_id, team_id, minutes_share, minutes_share_delta_prior "
            "FROM player_window_stats WHERE \"window\" = '10' AND game_date = :day "
            "AND team_id IS NOT NULL AND minutes_share_delta_prior IS NOT NULL"
        ),
        {"day": day},
    ).all()
    return top_movers([MoverRow(*row) for row in found], TOP_MINUTES_MOVERS)


def _impact_movers(
    session: Session, day: date, teams: dict[int, int]
) -> tuple[list[MoverRow], list[MoverRow]]:
    """The production rows on the date by each player's impact less its value 10 of his team games
    before, within the season, in the first source with both."""
    start, _ = season_bounds(season_label(day))
    columns = ", ".join(IMPACT_PREFERENCE)
    lagged = ", ".join(
        f"lag({source}, {MOVER_GAMES}) OVER games AS {source}_prior" for source in IMPACT_PREFERENCE
    )
    found = session.execute(
        text(
            f"SELECT * FROM (SELECT player_id, game_date, {columns}, {lagged} "
            "FROM player_window_stats WHERE \"window\" = '10' AND team_id IS NULL "
            "AND game_date BETWEEN :start AND :day "
            "WINDOW games AS (PARTITION BY player_id ORDER BY game_date)) lagged "
            "WHERE game_date = :day"
        ),
        {"start": start, "day": day},
    ).all()
    movers: list[MoverRow] = []
    for row in found:
        for source in IMPACT_PREFERENCE:
            now, then = getattr(row, source), getattr(row, f"{source}_prior")
            if now is not None and then is not None:
                movers.append(
                    MoverRow(row.player_id, teams.get(row.player_id), now, now - then, source)
                )
                break
    return top_movers(movers, TOP_IMPACT_MOVERS)


# Flags and transactions.


def _flags(session: Session, day: date) -> list[PlayerFlag]:
    found = session.scalars(select(PlayerFlag).where(PlayerFlag.game_date == day))
    return sorted(
        found,
        key=lambda flag: (
            _FLAG_RANK[flag.flag],
            by_nba_team_id(flag.team_id).tricode,
            flag.player_id,
        ),
    )


def _tier_strips(
    session: Session, day: date, keys: set[tuple[int, int]]
) -> dict[tuple[int, int], list[TierPoint]]:
    """Each player-team's tier on each of the team's last `STRIP_GAMES` final game dates through
    `day` this season, the oldest first, a date he has no `rotation_state` row on kept with a null
    tier, so the strip leaves its gap in place."""
    season = session.scalar(select(Game.season).where(Game.game_date == day).limit(1))
    if not keys or season is None:
        return {}
    team_ids = {team_id for _, team_id in keys}
    found = session.execute(
        select(Game.game_date, Game.home_team_id, Game.away_team_id)
        .where(Game.season == season, Game.game_date <= day, Game.status == "final")
        .where(or_(Game.home_team_id.in_(team_ids), Game.away_team_id.in_(team_ids)))
        .order_by(Game.game_date.desc())
    )
    dates: dict[int, set[date]] = defaultdict(set)
    for game_date, home, away in found:
        for team_id in (home, away):
            # A team plays once a date.
            if team_id in team_ids and len(dates[team_id]) < STRIP_GAMES:
                dates[team_id].add(game_date)
    tiers: dict[tuple[int, int, date], str] = {
        (player_id, team_id, game_date): tier
        for player_id, team_id, game_date, tier in session.execute(
            select(
                RotationState.player_id,
                RotationState.team_id,
                RotationState.game_date,
                RotationState.tier,
            )
            .where(tuple_(RotationState.player_id, RotationState.team_id).in_(list(keys)))
            .where(RotationState.game_date.in_({d for team in dates.values() for d in team}))
        )
    }
    return {
        (player_id, team_id): [
            TierPoint(
                game_date=game_date,
                tier=cast(Tier | None, tiers.get((player_id, team_id, game_date))),
            )
            for game_date in sorted(dates[team_id])
        ]
        for player_id, team_id in keys
    }


def _transactions(session: Session, day: date) -> list[Transaction]:
    """The date's and the day after's, the latter the morning the feed reads the date on."""
    return list(
        session.scalars(
            select(Transaction)
            .where(Transaction.date.between(day, day + timedelta(days=1)))
            .order_by(Transaction.date, Transaction.id)
        )
    )


# Injury changes.


class _Line(NamedTuple):
    status: str
    reason: str | None


class _Report(NamedTuple):
    report_datetime: datetime
    lines: dict[int, _Line]


class _Change(NamedTuple):
    player_id: int
    team_id: int
    before: _Line | None
    after: _Line | None
    report_datetime: datetime

    def model(self, names: dict[int, str]) -> InjuryChange:
        # A return's reason is what he is back from.
        line = self.after or self.before
        return InjuryChange(
            player=player_ref(self.player_id, names),
            tricode=by_nba_team_id(self.team_id).tricode,
            from_status=None if self.before is None else _status(self.before),
            to_status=None if self.after is None else _status(self.after),
            reason=None if line is None else line.reason,
            report_datetime=self.report_datetime,
        )


def _status(line: _Line) -> InjuryStatusName:
    return cast(InjuryStatusName, line.status)


def _injury_changes(session: Session, day: date) -> list[_Change]:
    """Each team's newest report by the end of `day` against its newest by the end of the league's
    previous game date this season (the day before, without one), so a report filed on a day no
    one played counts on the next: the players newly out, then those back, each by team and
    player."""
    start, _ = season_bounds(season_label(day))
    previous = session.scalar(
        select(func.max(Game.game_date)).where(
            Game.status == "final", Game.game_date >= start, Game.game_date < day
        )
    )
    now = _reports(session, day)
    then = _reports(session, previous or day - timedelta(days=1))
    changes: list[_Change] = []
    for team_id, report in now.items():
        before = then.get(team_id)
        if before is not None and before.report_datetime == report.report_datetime:
            continue
        old = {} if before is None else before.lines
        for player_id in {*report.lines, *old}:
            was, is_now = old.get(player_id), report.lines.get(player_id)
            was_out = was is not None and was.status in OUT
            is_out = is_now is not None and is_now.status in OUT
            back = is_now is None or is_now.status == "Available"
            if is_out != was_out and (is_out or back):
                changes.append(_Change(player_id, team_id, was, is_now, report.report_datetime))
    return sorted(
        changes,
        key=lambda change: (
            change.after is None or change.after.status not in OUT,
            by_nba_team_id(change.team_id).tricode,
            change.player_id,
        ),
    )


def _reports(session: Session, day: date) -> dict[int, _Report]:
    """Each team's newest injury report reported by the end of `day` in Eastern time, the official
    one on a tie with ESPN's, its injury lines for the nearest game it covers, as the team page's
    availability board reads one; a G League line is left out, being no injury."""
    end = datetime.combine(day + timedelta(days=1), time(), EASTERN)
    start, _ = season_bounds(season_label(day))
    newest = session.execute(
        select(InjuryStatus.team_id, InjuryStatus.report_datetime, InjuryStatus.source)
        .ext(distinct_on(InjuryStatus.team_id))
        .where(InjuryStatus.report_datetime < end, InjuryStatus.game_date >= start)
        .order_by(
            InjuryStatus.team_id,
            InjuryStatus.report_datetime.desc(),
            (InjuryStatus.source == PDF_SOURCE).desc(),
        )
    ).all()
    if not newest:
        return {}
    found = session.execute(
        select(
            InjuryStatus.team_id,
            InjuryStatus.game_date,
            InjuryStatus.player_id,
            InjuryStatus.status,
            InjuryStatus.reason,
        ).where(
            tuple_(InjuryStatus.team_id, InjuryStatus.report_datetime, InjuryStatus.source).in_(
                [tuple(report) for report in newest]
            )
        )
    ).all()
    nearest: dict[int, date] = {}
    for team_id, game_date, *_ in found:
        nearest[team_id] = min(game_date, nearest.get(team_id, game_date))
    lines: dict[int, dict[int, _Line]] = defaultdict(dict)
    for team_id, game_date, player_id, line_status, reason in found:
        if game_date == nearest[team_id] and reason not in NOT_INJURIES:
            lines[team_id][player_id] = _Line(line_status, reason)
    return {team_id: _Report(at, lines[team_id]) for team_id, at, _ in newest}
