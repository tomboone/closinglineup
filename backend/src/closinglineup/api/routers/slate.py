"""`GET /api/slate`: the games on a morning and each team's newest injury report for its game, read
off the schedule and the reports alone, nothing on it derived (v2 spec §5, §9):

- the **games** are the date's, by tip-off, each side with whether it is on the second night of a
  back-to-back;
- a team's **report** is its newest `injury_status` rows for the game (by `game_id`, or by team and
  date for a row not matched to one, as ESPN's are not) reported by the end of the date in Eastern
  time, the official one on a tie with ESPN's, as `derive.availability` picks one; its **previous**
  report is its newest for the same game by the end of the day before;
- the **lines** are the players on either report, each with his `change` between them (v2 spec
  §5.2), his tier and 10-game minutes share on the team's latest derived date on or before last
  night, and his impact on the date. A G League line is not an injury (spec §7.5): it is no line on
  either report, and an assignment's shows as the `g_league` badge, as `derive.availability` reads
  `g_league` from an assignment alone, never a two-way line. A team with no report shows the
  badges of its roster's players on assignment alone;
- **projected starters** are empty until NEU-1988 fills them.

`last_night` is the latest derived date before the date, the one the feed's other tabs read.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, NamedTuple, cast

from fastapi import APIRouter, Query
from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.orm import Session

from closinglineup.api.deps import SessionDep, latest_derived_date
from closinglineup.api.routers.teams import player_impacts, player_names, player_ref
from closinglineup.api.schemas import (
    Impact,
    InjurySource,
    LineChange,
    SlateGame,
    SlateLine,
    SlatePage,
    SlateReport,
    SlateTeam,
    Tier,
)
from closinglineup.api.schemas import (
    InjuryStatus as InjuryStatusName,
)
from closinglineup.db.models import (
    ROTATION_TIERS,
    Game,
    InjuryStatus,
    RosterMembership,
    RotationState,
)
from closinglineup.derive.availability import NOT_INJURIES
from closinglineup.derive.roster import ON_ASSIGNMENT, current_g_league_assignment
from closinglineup.injuries import ESPN_SOURCE, PDF_SOURCE
from closinglineup.sources.injury_pdf import EASTERN
from closinglineup.teams import by_nba_team_id

router = APIRouter()

# Worst first: the order lines are listed in, and what `upgraded` and `downgraded` compare.
STATUSES = ("Out", "Doubtful", "Questionable", "Probable", "Available")
# A player is out on a report at these statuses.
OUT = frozenset({"Out", "Doubtful"})
_STATUS_RANK = {line_status: rank for rank, line_status in enumerate(STATUSES)}
_TIER_RANK = {tier: rank for rank, tier in enumerate(ROTATION_TIERS)}


@router.get("/api/slate")
def slate(
    session: SessionDep,
    day: Annotated[
        date, Query(alias="date", description="The morning, a calendar date in Eastern time.")
    ],
) -> SlatePage:
    last_night = latest_derived_date(session, before=day)
    games = list(
        session.scalars(
            select(Game)
            .where(Game.game_date == day)
            .order_by(Game.tip_time.asc().nulls_last(), Game.game_id)
        )
    )
    team_ids = {team_id for game in games for team_id in (game.home_team_id, game.away_team_id)}
    reports = _reports(session, games, day)
    badges = {
        team_id: _assigned(session, team_id, day)
        for team_id in team_ids
        if reports.get(team_id, _Reports()).now is None
    }
    states = _states(session, team_ids, last_night)
    lines = {
        team_id: _lines(reports.get(team_id, _Reports()), badges.get(team_id, set()))
        for team_id in team_ids
    }
    players = {line.player_id for team in lines.values() for line in team}
    names = player_names(session, players)
    impacts = player_impacts(session, players, day)

    def side(team_id: int, b2b: bool) -> SlateTeam:
        now = reports.get(team_id, _Reports()).now
        team = [
            line.model(names, impacts, states.get((line.player_id, team_id)))
            for line in lines[team_id]
        ]
        return SlateTeam(
            tricode=by_nba_team_id(team_id).tricode,
            b2b=b2b,
            report=SlateReport(
                report_datetime=None,
                source=None,
                stale=False,
                submitted=False,
            )
            if now is None
            else SlateReport(
                report_datetime=now.report_datetime,
                source=cast(InjurySource, now.source),
                stale=now.source == ESPN_SOURCE,
                submitted=True,
            ),
            lines=sorted(team, key=_line_order),
            projected_starters=[],
        )

    return SlatePage(
        date=day,
        last_night=last_night,
        games=[
            SlateGame(
                game_id=game.game_id,
                tip_time=game.tip_time,
                home=side(game.home_team_id, game.home_b2b),
                away=side(game.away_team_id, game.away_b2b),
            )
            for game in games
        ],
    )


def _end_of(day: date) -> datetime:
    """Midnight Eastern at the start of the day after `day`."""
    return datetime.combine(day + timedelta(days=1), time(), EASTERN)


# Reports.


class _Line(NamedTuple):
    status: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class _Report:
    report_datetime: datetime
    source: str
    # His injury line, a G League line left out.
    lines: dict[int, _Line]
    # The players its lines put on G League assignment.
    g_league: frozenset[int]


@dataclass(frozen=True, slots=True)
class _Reports:
    now: _Report | None = None
    previous: _Report | None = None


def _reports(session: Session, games: Sequence[Game], day: date) -> dict[int, _Reports]:
    """Each slate team's newest report for its game by the end of `day` and by the end of the day
    before, the official one on a tie."""
    if not games:
        return {}
    team_ids = [team_id for game in games for team_id in (game.home_team_id, game.away_team_id)]
    found = session.execute(
        select(
            InjuryStatus.team_id,
            InjuryStatus.report_datetime,
            InjuryStatus.source,
            InjuryStatus.player_id,
            InjuryStatus.status,
            InjuryStatus.reason,
        ).where(
            InjuryStatus.team_id.in_(team_ids),
            InjuryStatus.report_datetime < _end_of(day),
            or_(
                InjuryStatus.game_id.in_([game.game_id for game in games]),
                and_(InjuryStatus.game_id.is_(None), InjuryStatus.game_date == day),
            ),
        )
    ).all()
    rows: dict[tuple[int, datetime, str], list[tuple[int, str, str | None]]] = defaultdict(list)
    for team_id, at, source, player_id, line_status, reason in found:
        rows[team_id, at, source].append((player_id, line_status, reason))

    def newest(team_id: int, end: datetime) -> _Report | None:
        keys = [key for key in rows if key[0] == team_id and key[1] < end]
        if not keys:
            return None
        _, at, source = max(keys, key=lambda key: (key[1], key[2] == PDF_SOURCE))
        listed = rows[team_id, at, source]
        return _Report(
            at,
            source,
            {
                player_id: _Line(line_status, reason)
                for player_id, line_status, reason in listed
                if reason not in NOT_INJURIES
            },
            frozenset(player_id for player_id, _, reason in listed if reason == ON_ASSIGNMENT),
        )

    return {
        team_id: _Reports(
            newest(team_id, _end_of(day)), newest(team_id, _end_of(day - timedelta(days=1)))
        )
        for team_id in team_ids
    }


def _assigned(session: Session, team_id: int, day: date) -> set[int]:
    """The players on the team's roster on `day` on G League assignment (`derive.roster`)."""
    roster = session.scalars(
        select(RosterMembership.player_id).where(
            RosterMembership.team_id == team_id,
            RosterMembership.start_date <= day,
            or_(RosterMembership.end_date.is_(None), RosterMembership.end_date >= day),
        )
    )
    return {
        player_id
        for player_id in set(roster)
        if current_g_league_assignment(session, player_id, day)
    }


def _states(
    session: Session, team_ids: Iterable[int], last_night: date | None
) -> dict[tuple[int, int], tuple[str, float | None]]:
    """Each player-team's tier and 10-game minutes share on the team's latest derived date on or
    before `last_night`."""
    team_ids = list(team_ids)
    if last_night is None or not team_ids:
        return {}
    latest = (
        select(RotationState.team_id, func.max(RotationState.game_date).label("game_date"))
        .where(RotationState.team_id.in_(team_ids), RotationState.game_date <= last_night)
        .group_by(RotationState.team_id)
        .subquery()
    )
    found = session.execute(
        select(
            RotationState.player_id,
            RotationState.team_id,
            RotationState.tier,
            RotationState.minutes_share_10,
        ).where(
            tuple_(RotationState.team_id, RotationState.game_date).in_(
                select(latest.c.team_id, latest.c.game_date)
            )
        )
    ).all()
    return {(player_id, team_id): (tier, share) for player_id, team_id, tier, share in found}


# Lines.


def line_change(was: str | None, now: str | None) -> LineChange | None:
    """A player's change from his status on the previous report to his status on this one, None
    for not on it (v2 spec §5.2): `new` when he was not on the previous one, or is now out having
    not been; `upgraded` and `downgraded` for a better and a worse status, a worse one short of
    out; `off` when he is off this one."""
    if now is None:
        return None if was is None else "off"
    if was is None or (was not in OUT and now in OUT):
        return "new"
    if _STATUS_RANK[now] > _STATUS_RANK[was]:
        return "upgraded"
    if _STATUS_RANK[now] < _STATUS_RANK[was] and now not in OUT:
        return "downgraded"
    return None


@dataclass(frozen=True, slots=True)
class _SlateLine:
    player_id: int
    now: _Line | None
    was: _Line | None
    g_league: bool

    def model(
        self,
        names: dict[int, str],
        impacts: dict[int, Impact],
        state: tuple[str, float | None] | None,
    ) -> SlateLine:
        tier, share = (None, None) if state is None else state
        # An `off` line's reason is the one he came off with.
        line = self.now or self.was
        return SlateLine(
            player=player_ref(self.player_id, names),
            status=None if self.now is None else _status(self.now.status),
            reason=None if line is None else line.reason,
            previous_status=None if self.was is None else _status(self.was.status),
            change=line_change(
                None if self.was is None else self.was.status,
                None if self.now is None else self.now.status,
            ),
            tier=cast(Tier | None, tier),
            minutes_share_10=share,
            g_league=self.g_league,
            impact=impacts.get(self.player_id),
        )


def _status(line_status: str) -> InjuryStatusName:
    return cast(InjuryStatusName, line_status)


def _lines(reports: _Reports, badges: set[int]) -> list[_SlateLine]:
    """Every player on either report or with a G League badge, one line each: a team with no
    report has its roster's badges alone."""
    now, previous = reports.now, reports.previous
    if now is None:
        return [_SlateLine(player_id, None, None, True) for player_id in badges]
    before: dict[int, _Line] = {} if previous is None else previous.lines
    return [
        _SlateLine(
            player_id, now.lines.get(player_id), before.get(player_id), player_id in now.g_league
        )
        for player_id in {*now.lines, *before, *now.g_league}
    ]


def _line_order(line: SlateLine) -> tuple[Any, ...]:
    """Lines by status, worst first, then a G League badge alone, then the `off` lines, each by
    status, tier and name."""
    group = 2 if line.change == "off" else 0 if line.status is not None else 1
    shown = line.status or line.previous_status
    return (
        group,
        len(STATUSES) if shown is None else _STATUS_RANK[shown],
        len(ROTATION_TIERS) if line.tier is None else _TIER_RANK[line.tier],
        line.player.name is None,
        line.player.name or "",
        line.player.player_id,
    )
