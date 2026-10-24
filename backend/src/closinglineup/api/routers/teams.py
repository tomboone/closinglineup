"""`GET /api/teams` and `GET /api/teams/{tricode}`: the 30 teams, and one team's page (spec §8).

The team page reads the team's final games of the date's season through the date, newest first:

- the **tier board** is its `rotation_state` on the latest of them (`tier_date`), each player with
  sparklines over the latest 20 from `player_window_stats`' 10-game windows. The view's on-court
  ratings are garbage-time filtered whatever `?garbage_time=` says (spec §6.4);
- the **closing lineups** are the team's last stint of each of the latest 5 decided by 10 or fewer;
- the **lineups** and **combos** are its 10 fives and 10 pairs with the most offensive possessions
  over all of them, and their numbers over the latest 10, from its stints: garbage time left out
  unless `?garbage_time=include`, a stint not yet classified counting either way, as in the view.
  A stint's defensive possessions are the other team's view of it;
- the **availability board** is the team's newest injury report by the end of the date, and the
  players on its roster that date on G League assignment (`derive.roster`);
- **roster counts** and **contract notes** are its roster spans covering the date, and those
  players' rows in the latest weekly contract snapshot on or before it with a guarantee date still
  to come.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import combinations
from typing import Any, NamedTuple, cast

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session, aliased

from closinglineup.api.deps import DateDep, SessionDep, latest_derived_date
from closinglineup.api.schemas import (
    Availability,
    AvailabilityBoard,
    AvailabilityRow,
    Badge,
    ClosingLineup,
    ContractNote,
    GarbageTime,
    InjurySource,
    LineupStats,
    PlayerRef,
    Point,
    RosterCounts,
    TeamPage,
    TeamsResponse,
    TeamSummary,
    Tier,
    TierBoardRow,
)
from closinglineup.api.schemas import (
    InjuryStatus as InjuryStatusName,
)
from closinglineup.db.models import (
    INJURY_STATUSES,
    ROTATION_TIERS,
    Contract,
    Game,
    InjuryStatus,
    Player,
    RosterMembership,
    RotationState,
    Stint,
)
from closinglineup.derive.roster import current_g_league_assignment
from closinglineup.injuries import ESPN_SOURCE, PDF_SOURCE
from closinglineup.season import season_label
from closinglineup.sources.injury_pdf import EASTERN
from closinglineup.teams import TEAMS, UnknownTeam, by_nba_team_id, by_tricode
from closinglineup.teams import Team as TeamRef

router = APIRouter()

SPARKLINE_GAMES = 20
TREND_GAMES = 10
CLOSING_GAMES = 5
# A game decided by at most this many points has a closing lineup worth showing.
CLOSE_MARGIN = 10
TOP_GROUPS = 10
# The CBA's roster limits: standard contracts, 10-day and Exhibit 10 among them, and two-way.
STANDARD_LIMIT = 15
TWO_WAY_LIMIT = 3
TWO_WAY = "two_way"
# The impact sparkline's source: the first of these the player has a value for.
IMPACT_PREFERENCE = ("epm", "darko", "bpm_local")
_TIER_RANK = {tier: rank for rank, tier in enumerate(ROTATION_TIERS)}
_STATUS_RANK = {name: rank for rank, name in enumerate(INJURY_STATUSES)}


@router.get("/api/teams")
def teams(session: SessionDep) -> TeamsResponse:
    """The 30 teams, by tricode, with their regular-season record in the season of the latest
    derived date (0-0 before any) and the latest date each has rotation state for."""
    latest = latest_derived_date(session)
    season = None if latest is None else season_label(latest)
    wins: dict[int, int] = defaultdict(int)
    losses: dict[int, int] = defaultdict(int)
    if season is not None:
        found = session.execute(
            select(Game.home_team_id, Game.away_team_id, Game.home_score, Game.away_score).where(
                Game.season == season,
                Game.phase == "regular",
                Game.status == "final",
                Game.home_score.is_not(None),
                Game.away_score.is_not(None),
            )
        ).all()
        for home, away, home_score, away_score in found:
            home_won = cast(int, home_score) > cast(int, away_score)
            winner, loser = (home, away) if home_won else (away, home)
            wins[winner] += 1
            losses[loser] += 1
    derived = dict(
        session.execute(
            select(RotationState.team_id, func.max(RotationState.game_date)).group_by(
                RotationState.team_id
            )
        ).all()
    )
    return TeamsResponse(
        season=season,
        teams=[
            _summary(team, wins[team.nba_team_id], losses[team.nba_team_id], derived)
            for team in sorted(TEAMS, key=lambda team: team.tricode)
        ],
    )


def _summary(team: TeamRef, wins: int, losses: int, derived: dict[int, date]) -> TeamSummary:
    return TeamSummary(
        tricode=team.tricode,
        name=team.name,
        conference=team.conference,
        division=team.division,
        wins=wins,
        losses=losses,
        latest_derived_date=derived.get(team.nba_team_id),
    )


@router.get("/api/teams/{tricode}")
def team_page(
    tricode: str, session: SessionDep, day: DateDep, garbage_time: GarbageTime = "exclude"
) -> TeamPage:
    try:
        team = by_tricode(tricode.upper())
    except UnknownTeam:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No team {tricode!r}") from None
    team_id = team.nba_team_id
    games = _team_games(session, team_id, day)
    roster = _roster(session, team_id, day)
    stints = _stints(session, team_id, [game.game_id for game in games], garbage_time)
    recent = {game.game_id for game in games[:TREND_GAMES]}
    board = _tier_board(session, team_id, games[:SPARKLINE_GAMES])
    closing = _closing(session, team_id, games)
    lineups = top_groups(stints, 5, recent)
    combos = top_groups(stints, 2, recent)
    contracts = _contracts(session, team_id, day, set(roster))
    availability = _availability(session, team_id, day, roster)
    names = player_names(
        session,
        {
            *(row.player_id for row in board),
            *(p for lineup in closing for p in lineup.players),
            *(p for group in (*lineups, *combos) for p in group.players),
            *(row.player_id for row in contracts),
            *(row.player_id for row in availability.players),
        },
    )
    return TeamPage(
        tricode=team.tricode,
        name=team.name,
        date=day,
        tier_date=games[0].game_date if games else None,
        garbage_time=garbage_time,
        tier_board=[row.model(names) for row in board],
        closing_lineups=[lineup.model(names) for lineup in closing],
        lineups=[group.model(names) for group in lineups],
        combos=[group.model(names) for group in combos],
        availability=availability.model(names),
        roster=RosterCounts(
            standard=sum(contract != TWO_WAY for contract in roster.values()),
            standard_limit=STANDARD_LIMIT,
            two_way=sum(contract == TWO_WAY for contract in roster.values()),
            two_way_limit=TWO_WAY_LIMIT,
        ),
        contracts=[row.model(names) for row in contracts],
    )


def player_ref(player_id: int, names: dict[int, str]) -> PlayerRef:
    return PlayerRef(player_id=player_id, name=names.get(player_id))


def player_names(session: Session, player_ids: set[int]) -> dict[int, str]:
    if not player_ids:
        return {}
    return dict(
        session.execute(
            select(Player.nba_player_id, Player.name).where(Player.nba_player_id.in_(player_ids))
        ).all()
    )


class _TeamGame(NamedTuple):
    game_id: str
    game_date: date
    opponent_id: int
    # The team's final margin; None while a score is missing.
    margin: int | None


def _team_games(session: Session, team_id: int, day: date) -> list[_TeamGame]:
    """The team's final games of `day`'s season through `day`, newest first."""
    found = session.execute(
        select(
            Game.game_id,
            Game.game_date,
            Game.home_team_id,
            Game.away_team_id,
            Game.home_score,
            Game.away_score,
        )
        .where(
            Game.season == season_label(day),
            Game.status == "final",
            Game.game_date <= day,
            or_(Game.home_team_id == team_id, Game.away_team_id == team_id),
        )
        .order_by(Game.game_date.desc(), Game.game_id.desc())
    ).all()
    games: list[_TeamGame] = []
    for game_id, game_date, home, away, home_score, away_score in found:
        margin = None
        if home_score is not None and away_score is not None:
            margin = home_score - away_score if home == team_id else away_score - home_score
        games.append(_TeamGame(game_id, game_date, away if home == team_id else home, margin))
    return games


def _roster(session: Session, team_id: int, day: date) -> dict[int, str]:
    """Each player on the team's roster on `day`, to his contract type."""
    return dict(
        session.execute(
            select(RosterMembership.player_id, RosterMembership.contract_type).where(
                RosterMembership.team_id == team_id,
                RosterMembership.start_date <= day,
                or_(RosterMembership.end_date.is_(None), RosterMembership.end_date >= day),
            )
        ).all()
    )


# Tier board.


@dataclass(frozen=True, slots=True)
class _BoardRow:
    player_id: int
    state: RotationState
    minutes_share: list[Point]
    on_off_net: list[Point]
    impact: list[Point]
    impact_source: str | None

    def model(self, names: dict[int, str]) -> TierBoardRow:
        state = self.state
        return TierBoardRow(
            player=player_ref(self.player_id, names),
            tier=cast(Tier, state.tier),
            badges=[cast(Badge, badge) for badge in state.badges],
            availability=cast(Availability, state.availability),
            is_short=state.is_short,
            minutes_share_10=state.minutes_share_10,
            starter_rate_5=state.starter_rate_5,
            played_rate_10=state.played_rate_10,
            minutes_share=self.minutes_share,
            on_off_net=self.on_off_net,
            impact=self.impact,
            impact_source=cast(Any, self.impact_source),
        )


def _tier_board(session: Session, team_id: int, games: Sequence[_TeamGame]) -> list[_BoardRow]:
    """The team's rotation state on its latest game date, by tier then 10-game minutes share, with
    each player's sparklines over `games`."""
    if not games:
        return []
    states = list(
        session.scalars(
            select(RotationState).where(
                RotationState.team_id == team_id, RotationState.game_date == games[0].game_date
            )
        )
    )
    dates = sorted(game.game_date for game in games)
    found = session.execute(
        text(
            "SELECT player_id, team_id, game_date, minutes_share, on_off_net, "
            "epm, darko, bpm_local "
            "FROM player_window_stats WHERE \"window\" = '10' AND game_date = ANY(:dates) "
            "AND player_id = ANY(:players) AND (team_id = :team OR team_id IS NULL)"
        ),
        {"dates": dates, "players": [state.player_id for state in states], "team": team_id},
    ).all()
    role: dict[int, dict[date, Any]] = defaultdict(dict)
    production: dict[int, dict[date, Any]] = defaultdict(dict)
    for row in found:
        (production if row.team_id is None else role)[row.player_id][row.game_date] = row
    rows: list[_BoardRow] = []
    for state in states:
        played = role[state.player_id]
        impacts = production[state.player_id]
        source = next(
            (
                name
                for name in IMPACT_PREFERENCE
                if any(getattr(row, name) is not None for row in impacts.values())
            ),
            None,
        )
        rows.append(
            _BoardRow(
                player_id=state.player_id,
                state=state,
                minutes_share=_series(dates, played, "minutes_share"),
                on_off_net=_series(dates, played, "on_off_net"),
                impact=[] if source is None else _series(dates, impacts, source),
                impact_source=source,
            )
        )
    return sorted(
        rows,
        key=lambda row: (
            _TIER_RANK[row.state.tier],
            -(row.state.minutes_share_10 or 0.0),
            row.player_id,
        ),
    )


def _series(dates: Iterable[date], rows: dict[date, Any], column: str) -> list[Point]:
    """One point per date the player has a row for, oldest first."""
    return [Point(game_date=day, value=getattr(rows[day], column)) for day in dates if day in rows]


# Closing lineups.


@dataclass(frozen=True, slots=True)
class _Closing:
    game: _TeamGame
    players: tuple[int, ...]

    def model(self, names: dict[int, str]) -> ClosingLineup:
        return ClosingLineup(
            game_id=self.game.game_id,
            game_date=self.game.game_date,
            opponent=by_nba_team_id(self.game.opponent_id).tricode,
            margin=cast(int, self.game.margin),
            players=[player_ref(player_id, names) for player_id in self.players],
        )


def _closing(session: Session, team_id: int, games: Sequence[_TeamGame]) -> list[_Closing]:
    """The team's five at the final buzzer, its last stint, in each of its latest games decided by
    `CLOSE_MARGIN` or fewer that have stints, at most `CLOSING_GAMES` of them, newest first."""
    close = [game for game in games if game.margin is not None and abs(game.margin) <= CLOSE_MARGIN]
    if not close:
        return []
    last = dict(
        session.execute(
            select(Stint.game_id, Stint.lineup_key)
            .ext(distinct_on(Stint.game_id))
            .where(Stint.team_id == team_id, Stint.game_id.in_([game.game_id for game in close]))
            .order_by(Stint.game_id, Stint.period.desc(), Stint.end_sec.desc())
        ).all()
    )
    found = [
        _Closing(game, lineup_members(last[game.game_id])) for game in close if game.game_id in last
    ]
    return found[:CLOSING_GAMES]


def lineup_members(lineup_key: str) -> tuple[int, ...]:
    return tuple(int(member) for member in lineup_key.split("-"))


# Lineups and combos.


class StintRow(NamedTuple):
    """A team's stint with its defensive possessions, as the lineup tables sum them."""

    game_id: str
    lineup_key: str
    possessions: int
    opp_possessions: int
    pts_for: int
    pts_against: int


@dataclass(slots=True)
class Totals:
    possessions: int = 0
    opp_possessions: int = 0
    pts_for: int = 0
    pts_against: int = 0

    def add(self, stint: StintRow) -> None:
        self.possessions += stint.possessions
        self.opp_possessions += stint.opp_possessions
        self.pts_for += stint.pts_for
        self.pts_against += stint.pts_against

    @property
    def net_rating(self) -> float | None:
        """Points scored per 100 offensive possessions less points allowed per 100 defensive ones;
        None without both."""
        if self.possessions == 0 or self.opp_possessions == 0:
            return None
        return 100 * self.pts_for / self.possessions - 100 * self.pts_against / self.opp_possessions


@dataclass(frozen=True, slots=True)
class Group:
    """A five or a pair: its totals over every stint given, and over the recent games'."""

    players: tuple[int, ...]
    season: Totals
    recent: Totals

    def model(self, names: dict[int, str]) -> LineupStats:
        return LineupStats(
            players=[player_ref(player_id, names) for player_id in self.players],
            possessions=self.season.possessions,
            opp_possessions=self.season.opp_possessions,
            net_rating=self.season.net_rating,
            possessions_10=self.recent.possessions,
            net_rating_10=self.recent.net_rating,
        )


def top_groups(
    stints: Iterable[StintRow], size: int, recent: set[str], limit: int = TOP_GROUPS
) -> list[Group]:
    """The `limit` groups of `size` players sharing the floor in `stints` with the most offensive
    possessions, then by player IDs; each with its totals over the stints of the `recent` games
    too."""
    season: dict[tuple[int, ...], Totals] = defaultdict(Totals)
    latest: dict[tuple[int, ...], Totals] = defaultdict(Totals)
    for stint in stints:
        for group in combinations(sorted(lineup_members(stint.lineup_key)), size):
            season[group].add(stint)
            if stint.game_id in recent:
                latest[group].add(stint)
    ranked = sorted(season.items(), key=lambda item: (-item[1].possessions, item[0]))
    return [Group(group, totals, latest.get(group, Totals())) for group, totals in ranked[:limit]]


def _stints(
    session: Session, team_id: int, game_ids: list[str], garbage_time: GarbageTime
) -> list[StintRow]:
    if not game_ids:
        return []
    other = aliased(Stint)
    query = (
        select(
            Stint.game_id,
            Stint.lineup_key,
            Stint.possessions,
            func.coalesce(other.possessions, 0),
            Stint.pts_for,
            Stint.pts_against,
        )
        .outerjoin(
            other,
            and_(
                other.game_id == Stint.game_id,
                other.period == Stint.period,
                other.start_sec == Stint.start_sec,
                other.team_id != Stint.team_id,
            ),
        )
        .where(Stint.team_id == team_id, Stint.game_id.in_(game_ids))
    )
    if garbage_time == "exclude":
        query = query.where(Stint.is_garbage_time.is_not(True))
    return [StintRow(*row) for row in session.execute(query).all()]


# Availability board.


@dataclass(frozen=True, slots=True)
class _Listed:
    player_id: int
    status: str | None
    reason: str | None
    g_league: bool

    def model(self, names: dict[int, str]) -> AvailabilityRow:
        return AvailabilityRow(
            player=player_ref(self.player_id, names),
            status=cast(InjuryStatusName | None, self.status),
            reason=self.reason,
            g_league=self.g_league,
        )


@dataclass(frozen=True, slots=True)
class _Board:
    report_datetime: datetime | None
    game_date: date | None
    source: str | None
    players: list[_Listed]

    def model(self, names: dict[int, str]) -> AvailabilityBoard:
        return AvailabilityBoard(
            report_datetime=self.report_datetime,
            game_date=self.game_date,
            source=cast(InjurySource | None, self.source),
            stale=self.source == ESPN_SOURCE,
            players=[listed.model(names) for listed in self.players],
        )


def _availability(session: Session, team_id: int, day: date, roster: dict[int, str]) -> _Board:
    """The team's newest injury report reported by the end of `day` in Eastern time, the official
    one on a tie with ESPN's, as `derive.availability` picks it, its lines for the nearest game it
    covers (a report on the night of a back-to-back lists both) by status then player; then the
    rostered players on G League assignment it does not list."""
    end = datetime.combine(day + timedelta(days=1), time(), EASTERN)
    report = session.execute(
        select(InjuryStatus.report_datetime, InjuryStatus.source, InjuryStatus.game_date)
        .where(InjuryStatus.team_id == team_id, InjuryStatus.report_datetime < end)
        .order_by(
            InjuryStatus.report_datetime.desc(),
            (InjuryStatus.source == PDF_SOURCE).desc(),
            InjuryStatus.game_date,
        )
        .limit(1)
    ).first()
    assigned = {
        player_id for player_id in roster if current_g_league_assignment(session, player_id, day)
    }
    lines: list[tuple[int, str, str | None]] = []
    if report is not None:
        lines = sorted(
            session.execute(
                select(InjuryStatus.player_id, InjuryStatus.status, InjuryStatus.reason).where(
                    InjuryStatus.team_id == team_id,
                    InjuryStatus.report_datetime == report.report_datetime,
                    InjuryStatus.source == report.source,
                    InjuryStatus.game_date == report.game_date,
                )
            ).all(),
            key=lambda line: (_STATUS_RANK[line[1]], line[0]),
        )
    listed = [
        _Listed(player_id, line_status, reason, player_id in assigned)
        for player_id, line_status, reason in lines
    ]
    on_report = {line.player_id for line in listed}
    listed += [_Listed(player_id, None, None, True) for player_id in sorted(assigned - on_report)]
    if report is None:
        return _Board(None, None, None, listed)
    return _Board(report.report_datetime, report.game_date, report.source, listed)


# Contract notes.


@dataclass(frozen=True, slots=True)
class _Note:
    player_id: int
    contract: Contract
    days: int

    def model(self, names: dict[int, str]) -> ContractNote:
        contract = self.contract
        return ContractNote(
            player=player_ref(self.player_id, names),
            salary=contract.salary,
            minimum_salary=contract.minimum_salary,
            guaranteed=contract.guaranteed,
            guarantee_date=contract.guarantee_date,
            guaranteed_after=contract.guaranteed_after,
            days_until_guarantee=self.days,
        )


def _contracts(session: Session, team_id: int, day: date, roster: set[int]) -> list[_Note]:
    """The rostered players' contracts with the team in the latest snapshot on or before `day`
    whose guarantee date is on or after it, the soonest first, as `derive.flags` reads them."""
    as_of = session.scalar(select(func.max(Contract.as_of_date)).where(Contract.as_of_date <= day))
    if as_of is None or not roster:
        return []
    found = session.scalars(
        select(Contract)
        .where(
            Contract.as_of_date == as_of,
            Contract.team_id == team_id,
            Contract.player_id.in_(roster),
            Contract.guarantee_date >= day,
        )
        .order_by(Contract.guarantee_date, Contract.player_id)
    )
    return [
        _Note(contract.player_id, contract, (contract.guarantee_date - day).days)
        for contract in found
    ]
