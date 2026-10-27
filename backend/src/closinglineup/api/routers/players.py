"""`GET /api/players/{id}`: a player's page (spec §8).

The page reads the player's team games: the final games of the date's season through the date that
a team of his played while he was on its roster, as `player_window_stats` keys its role rows, oldest
first. `?window=` keeps the latest 5, 10 or 20 of them, or all for `season`:

- the **header** is his `rotation_state` on the latest of them, his tier and availability; with
  none, the team whose roster he is on that date, and no tier; and his line on the newest injury
  report by the date of the team whose roster he is on, as the team page reads one;
- the **series** have a point per game in the window: his minutes and share of his team's
  box-score seconds; his team's net rating with him on the floor and that less its net with him
  off, from its stints, garbage time left out unless `?garbage_time=include` as on the team page;
  his TS% and USG%, null in a game he did not play; and each impact source's latest snapshot by the
  date;
- the **tiers** are his `rotation_state` tier on each game in the window, null where he has none;
- the **annotations** are the changes on the window's dates: his status on the injury reports of
  the team whose roster he is on, a report covering it that omits him after one listing him taking
  him off it, and ESPN's fallback read only where no official report covers the game; his
  transactions; his rotation events; and whether he started, or closed a game with a closing
  situation (spec §7.1, as `player_window_stats` reads one), against the previous game he played
  this season;
- the **shot profile** is `player_window_stats`' production rows on the latest game for the window,
  the season and the prior season, or before his first game of the season the prior season's only;
- the **production** table reads the same rows for every window at once, a row per metric (v2 spec
  §8.6), Win Shares and VORP for the season and the prior season only;
- the **teammates** are the 5 who shared the floor with him for the most offensive possessions in
  the window's stints, garbage time as for the series, with the net rating while they did;
- the **matchups** are the 5 defenders who guarded him, and the 5 players he guarded, for the most
  partial possessions over the window's games;
- the **flags** are those raised on him this season through the date, newest first;
- the **contract** is his with the team whose roster he is on, as its page's contract notes read
  one.
"""

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from math import floor
from typing import Any, NamedTuple, cast

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import and_, exists, func, or_, select, text, tuple_
from sqlalchemy.orm import Session, aliased

from closinglineup.api.deps import DateDep, SessionDep
from closinglineup.api.routers.teams import (
    StintRow,
    Totals,
    contract_note,
    contract_notes,
    lineup_members,
    newest_report,
    player_names,
    player_ref,
)
from closinglineup.api.schemas import (
    Annotation,
    Availability,
    Badge,
    Flag,
    GarbageTime,
    ImpactSeries,
    InjuryLine,
    MatchupRow,
    Matchups,
    PlayerHeader,
    PlayerPage,
    PlayerSeries,
    Point,
    ProductionCell,
    ProductionFormat,
    ProductionMetric,
    ProductionRow,
    ProductionWindows,
    RaisedFlag,
    ShotZone,
    Teammate,
    Tier,
    TierPoint,
    Window,
)
from closinglineup.api.schemas import (
    InjuryStatus as InjuryStatusName,
)
from closinglineup.db.models import (
    IMPACT_RATINGS,
    SHOT_ZONES,
    VALUE_METRICS,
    Contract,
    ImpactSnapshot,
    InjuryStatus,
    Matchup,
    Player,
    PlayerFlag,
    PlayerGame,
    RosterMembership,
    RotationEvent,
    RotationState,
    Stint,
    Transaction,
)
from closinglineup.injuries import PDF_SOURCE
from closinglineup.season import season_bounds, season_label
from closinglineup.sources.injury_pdf import EASTERN
from closinglineup.teams import by_nba_team_id

router = APIRouter()

WINDOW_GAMES = {"5": 5, "10": 10, "20": 20}
TOP_TEAMMATES = 5
TOP_MATCHUPS = 5
# Spec §7.1's closing situation, as `player_window_stats` reads it: a stint of the final five
# minutes of the game's last period that starts with the margin closer than 10.
CLOSING_SECONDS = 300
CLOSE_MARGIN = 10
# A team's available player-seconds in regulation and in each overtime (spec §7.1).
REGULATION_SECONDS = 5 * 2880
OVERTIME_SECONDS = 5 * 300
OFF_REPORT = "Off the injury report"
PRODUCTION_WINDOWS = ("5", "10", "20", "season", "prior_season")
# VORP is snapshotted as a season-to-date total and kept for these windows only (NEU-2003).
SEASON_WINDOWS = ("season", "prior_season")
_KIND_RANK = {"injury": 0, "transaction": 1, "tier": 2, "starter": 3, "closer": 4}


class _Game(NamedTuple):
    game_id: str
    game_date: date
    team_id: int


class _Line(NamedTuple):
    seconds: int
    is_starter: bool
    ts_pct: float | None
    usg_pct: float | None


@dataclass(frozen=True, slots=True)
class _Stint:
    """His team's stint, its members parsed."""

    row: StintRow
    period: int
    start_sec: float
    end_sec: float
    members: frozenset[int]
    is_garbage_time: bool | None

    def counts(self, garbage_time: GarbageTime) -> bool:
        """Whether the on-court numbers sum it; an unclassified stint counts either way."""
        return garbage_time == "include" or self.is_garbage_time is not True


@router.get("/api/players/{player_id}")
def player_page(
    player_id: int,
    session: SessionDep,
    day: DateDep,
    window: Window = "10",
    garbage_time: GarbageTime = "exclude",
) -> PlayerPage:
    player = session.get(Player, player_id)
    games = _games(session, player_id, day)
    if player is None and not games and not _known(session, player_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No player {player_id}")
    shown = games if window == "season" else games[-WINDOW_GAMES[window] :]
    since = shown[0].game_date if shown else day
    lines = _lines(session, player_id, games)
    stints = _stints(session, games)
    teammates = _teammates(player_id, shown, stints, garbage_time)
    defenders, guarded = _matchups(session, player_id, [game.game_id for game in shown])
    flags = _flags(session, player_id, day)
    rostered = _rostered_team(session, player_id, day)
    contract = _contract(session, player_id, rostered, day)
    names = player_names(
        session,
        {
            player_id,
            *(mate for mate, _ in teammates),
            *(row.player_id for row in (*defenders, *guarded)),
        },
    )
    latest = games[-1].game_date if games else None
    annotations = [
        *_injuries(session, player_id, games, day),
        *_transactions(session, player_id, day),
        *_tier_events(session, player_id, since, day),
        *_roles(player_id, games, lines, stints),
    ]
    return PlayerPage(
        date=day,
        window=window,
        garbage_time=garbage_time,
        header=_header(session, player_id, player, rostered, games, day, names),
        series=_series(session, player_id, shown, lines, stints, garbage_time, day),
        tiers=_tiers(session, player_id, shown),
        annotations=sorted(
            (note for note in annotations if since <= note.game_date <= day),
            key=lambda note: (note.game_date, _KIND_RANK[note.kind]),
        ),
        shot_profile=_shot_profile(session, player_id, latest, window, day),
        production=_production_table(session, player_id, latest, day),
        teammates=[
            Teammate(
                player=player_ref(mate, names),
                possessions=totals.possessions,
                net_rating=totals.net_rating,
            )
            for mate, totals in teammates
        ],
        matchups=Matchups(
            defenders=[row.model(names) for row in defenders],
            guarded=[row.model(names) for row in guarded],
        ),
        flags=[
            RaisedFlag(
                player=player_ref(player_id, names),
                tricode=by_nba_team_id(flag.team_id).tricode,
                game_date=flag.game_date,
                flag=cast(Flag, flag.flag),
                detail=flag.detail,
                # The feed's alone (v2 spec §6.2, §7); this page charts his impact series.
                impact=None,
                tiers=None,
            )
            for flag in flags
        ],
        contract=None if contract is None else contract_note(contract, day, names),
    )


def _known(session: Session, player_id: int) -> bool:
    """Whether a box score or a roster has him, his `player` row aside."""
    return bool(
        session.scalar(
            select(
                or_(
                    exists().where(PlayerGame.player_id == player_id),
                    exists().where(RosterMembership.player_id == player_id),
                )
            )
        )
    )


def _games(session: Session, player_id: int, day: date) -> list[_Game]:
    """His team games of `day`'s season through `day`, oldest first."""
    found = session.execute(
        text(
            "SELECT DISTINCT ON (g.game_date) g.game_id, g.game_date, v.team_id "
            "FROM player_window_stats v JOIN game g ON g.game_date = v.game_date "
            "AND v.team_id IN (g.home_team_id, g.away_team_id) "
            "WHERE v.player_id = :player AND v.team_id IS NOT NULL AND v.\"window\" = 'season' "
            "AND v.game_date <= :day AND g.season = :season AND g.status = 'final' "
            "ORDER BY g.game_date, g.game_id"
        ),
        {"player": player_id, "day": day, "season": season_label(day)},
    ).all()
    return [_Game(*row) for row in found]


def _team_pairs(games: Iterable[_Game]) -> list[tuple[str, int]]:
    return [(game.game_id, game.team_id) for game in games]


def _lines(session: Session, player_id: int, games: Sequence[_Game]) -> dict[str, _Line]:
    """His box-score line in each game that has one."""
    if not games:
        return {}
    found = session.execute(
        select(
            PlayerGame.game_id,
            PlayerGame.seconds,
            PlayerGame.is_starter,
            PlayerGame.ts_pct,
            PlayerGame.usg_pct,
        ).where(
            PlayerGame.player_id == player_id,
            PlayerGame.game_id.in_([game.game_id for game in games]),
        )
    ).all()
    return {
        game_id: _Line(seconds, is_starter, ts_pct, usg_pct)
        for game_id, seconds, is_starter, ts_pct, usg_pct in found
    }


def _stints(session: Session, games: Sequence[_Game]) -> dict[str, list[_Stint]]:
    """His team's stints in each game, in order, with their defensive possessions, as the team
    page reads them."""
    if not games:
        return {}
    other = aliased(Stint)
    found = session.execute(
        select(
            Stint.game_id,
            Stint.lineup_key,
            Stint.possessions,
            func.coalesce(other.possessions, 0),
            Stint.pts_for,
            Stint.pts_against,
            Stint.period,
            Stint.start_sec,
            Stint.end_sec,
            Stint.is_garbage_time,
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
        .where(tuple_(Stint.game_id, Stint.team_id).in_(_team_pairs(games)))
        .order_by(Stint.game_id, Stint.period, Stint.start_sec)
    ).all()
    stints: dict[str, list[_Stint]] = defaultdict(list)
    for (
        game_id,
        lineup_key,
        possessions,
        opp_possessions,
        pts_for,
        pts_against,
        period,
        start_sec,
        end_sec,
        garbage,
    ) in found:
        row = StintRow(game_id, lineup_key, possessions, opp_possessions, pts_for, pts_against)
        stints[game_id].append(
            _Stint(
                row,
                period,
                start_sec,
                end_sec,
                frozenset(lineup_members(lineup_key)),
                garbage,
            )
        )
    return stints


# Header.


def _rostered_team(session: Session, player_id: int, day: date) -> int | None:
    """The team whose roster he is on on `day`, if any."""
    return session.scalar(
        select(RosterMembership.team_id).where(
            RosterMembership.player_id == player_id,
            RosterMembership.start_date <= day,
            or_(RosterMembership.end_date.is_(None), RosterMembership.end_date >= day),
        )
    )


def _header(
    session: Session,
    player_id: int,
    player: Player | None,
    rostered: int | None,
    games: Sequence[_Game],
    day: date,
    names: dict[int, str],
) -> PlayerHeader:
    """His team and state at his latest team game, or before his first the team whose roster he
    is on; his injury line is always that roster team's, which a trade can make another."""
    state = None
    team_id = rostered
    if games:
        latest = games[-1]
        team_id = latest.team_id
        state = session.get(RotationState, (player_id, latest.team_id, latest.game_date))
    return PlayerHeader(
        player=player_ref(player_id, names),
        position=None if player is None else player.position,
        tricode=None if team_id is None else by_nba_team_id(team_id).tricode,
        tier=None if state is None else cast(Tier, state.tier),
        availability=None if state is None else cast(Availability, state.availability),
        badges=[] if state is None else [cast(Badge, badge) for badge in state.badges],
        injury=None if rostered is None else _injury_line(session, player_id, rostered, day),
    )


def _injury_line(session: Session, player_id: int, team_id: int, day: date) -> InjuryLine | None:
    """His line on the team's newest injury report by the end of `day`, as the team page's
    availability board reads it."""
    report = newest_report(session, team_id, day)
    if report is None:
        return None
    line = session.execute(report.lines(team_id, InjuryStatus.player_id == player_id)).first()
    if line is None:
        return None
    _, line_status, reason = line
    return InjuryLine(status=cast(InjuryStatusName, line_status), reason=reason)


def _contract(session: Session, player_id: int, rostered: int | None, day: date) -> Contract | None:
    """His contract with the team whose roster he is on, as its page's contract notes read one."""
    if rostered is None:
        return None
    found = contract_notes(session, rostered, day, {player_id})
    return found[0] if found else None


# Tiers.


def _tiers(session: Session, player_id: int, shown: Sequence[_Game]) -> list[TierPoint]:
    """His tier on each game's date with the team he played it for, null where he has no row."""
    if not shown:
        return []
    found = dict(
        session.execute(
            select(RotationState.game_date, RotationState.tier).where(
                RotationState.player_id == player_id,
                tuple_(RotationState.team_id, RotationState.game_date).in_(
                    [(game.team_id, game.game_date) for game in shown]
                ),
            )
        ).all()
    )
    return [
        TierPoint(game_date=game.game_date, tier=cast(Tier | None, found.get(game.game_date)))
        for game in shown
    ]


# Series.


def _series(
    session: Session,
    player_id: int,
    shown: Sequence[_Game],
    lines: dict[str, _Line],
    stints: dict[str, list[_Stint]],
    garbage_time: GarbageTime,
    day: date,
) -> PlayerSeries:
    team_seconds = _team_seconds(session, shown)
    minutes: list[Point] = []
    share: list[Point] = []
    on_court: list[Point] = []
    on_off: list[Point] = []
    ts_pct: list[Point] = []
    usg_pct: list[Point] = []
    for game in shown:
        line = lines.get(game.game_id)
        seconds = 0 if line is None else line.seconds
        played = line if line is not None and line.seconds > 0 else None
        total = team_seconds.get(game.game_id)
        on, off = Totals(), Totals()
        for stint in stints.get(game.game_id, []):
            if stint.counts(garbage_time):
                (on if player_id in stint.members else off).add(stint.row)
        on_net, off_net = on.net_rating, off.net_rating
        on_off_net = None if on_net is None or off_net is None else on_net - off_net
        when = game.game_date
        minutes.append(Point(game_date=when, value=seconds / 60))
        share.append(Point(game_date=when, value=seconds / total if total else None))
        on_court.append(Point(game_date=when, value=on_net))
        on_off.append(Point(game_date=when, value=on_off_net))
        ts_pct.append(Point(game_date=when, value=None if played is None else played.ts_pct))
        usg_pct.append(Point(game_date=when, value=None if played is None else played.usg_pct))
    return PlayerSeries(
        minutes=minutes,
        minutes_share=share,
        on_court_net=on_court,
        on_off_net=on_off,
        ts_pct=ts_pct,
        usg_pct=usg_pct,
        impact=_impact(session, player_id, [game.game_date for game in shown], day),
    )


def _team_seconds(session: Session, games: Sequence[_Game]) -> dict[str, int]:
    """His team's available player-seconds in each game, as `player_window_stats` reads them:
    five players over 48 minutes plus 5 for each overtime, the overtimes read off the box score's
    seconds, which sum to five times the game's length give or take rounding (spec §7.1)."""
    if not games:
        return {}
    found = session.execute(
        select(PlayerGame.game_id, func.sum(PlayerGame.seconds))
        .where(tuple_(PlayerGame.game_id, PlayerGame.team_id).in_(_team_pairs(games)))
        .group_by(PlayerGame.game_id)
    ).all()
    # Postgres rounds half away from zero; Python's `round` to even.
    return {
        game_id: REGULATION_SECONDS
        + OVERTIME_SECONDS * floor(max(seconds - REGULATION_SECONDS, 0) / OVERTIME_SECONDS + 0.5)
        for game_id, seconds in found
    }


def _impact(
    session: Session, player_id: int, dates: Sequence[date], day: date
) -> list[ImpactSeries]:
    """Each source's latest snapshot at or before each date, for the sources with one."""
    if not dates:
        return []
    found = session.execute(
        select(ImpactSnapshot.source, ImpactSnapshot.date, ImpactSnapshot.value)
        .where(ImpactSnapshot.player_id == player_id, ImpactSnapshot.date <= day)
        .order_by(ImpactSnapshot.date)
    ).all()
    snapshots: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for source, when, value in found:
        snapshots[source].append((when, value))
    series: list[ImpactSeries] = []
    for source in IMPACT_RATINGS:
        taken = snapshots.get(source, [])
        taken_dates = [when for when, _ in taken]
        points = [
            Point(game_date=when, value=taken[at - 1][1])
            for when in dates
            if (at := bisect_right(taken_dates, when)) > 0
        ]
        if points:
            series.append(ImpactSeries(source=cast(Any, source), points=points))
    return series


# Annotations.


def _team_on(spans: Sequence[tuple[int, date, date | None]], day: date) -> int | None:
    """His team on `day`: that of the roster span covering it, if any."""
    for team_id, start, end in spans:
        if start <= day and (end is None or day <= end):
            return team_id
    return None


def _injuries(
    session: Session, player_id: int, games: Sequence[_Game], day: date
) -> list[Annotation]:
    """A note each time his status changes from one report covering the team whose roster he is on
    to the next, by the end of `day` in Eastern time this season: each report's lines for the
    nearest game it covers, ESPN's fallback only for a game no official report covers."""
    if not games:
        return []
    spans = session.execute(
        select(RosterMembership.team_id, RosterMembership.start_date, RosterMembership.end_date)
        .where(RosterMembership.player_id == player_id)
        .order_by(RosterMembership.start_date)
    ).all()
    window = (
        InjuryStatus.team_id.in_({team_id for team_id, _, _ in spans}),
        InjuryStatus.report_datetime < datetime.combine(day + timedelta(days=1), time(), EASTERN),
        InjuryStatus.game_date >= games[0].game_date,
    )
    reports = session.execute(
        select(
            InjuryStatus.team_id,
            InjuryStatus.report_datetime,
            InjuryStatus.source,
            func.min(InjuryStatus.game_date),
        )
        .where(*window)
        .group_by(InjuryStatus.team_id, InjuryStatus.report_datetime, InjuryStatus.source)
        .order_by(InjuryStatus.report_datetime, (InjuryStatus.source == PDF_SOURCE).desc())
    ).all()
    listed = {
        (team_id, at, source, game_date): (line_status, reason)
        for team_id, at, source, game_date, line_status, reason in session.execute(
            select(
                InjuryStatus.team_id,
                InjuryStatus.report_datetime,
                InjuryStatus.source,
                InjuryStatus.game_date,
                InjuryStatus.status,
                InjuryStatus.reason,
            ).where(*window, InjuryStatus.player_id == player_id)
        ).all()
    }
    official = {
        (team_id, game_date) for team_id, _, source, game_date in reports if source == PDF_SOURCE
    }
    notes: list[Annotation] = []
    current: str | None = None
    for team_id, at, source, game_date in reports:
        if team_id != _team_on(spans, game_date) or (
            source != PDF_SOURCE and (team_id, game_date) in official
        ):
            continue
        line = listed.get((team_id, at, source, game_date))
        now = None if line is None else line[0]
        if now == current:
            continue
        current = now
        if line is None:
            label = OFF_REPORT
        else:
            label = line[0] if line[1] is None else f"{line[0]}: {line[1]}"
        notes.append(Annotation(game_date=game_date, kind="injury", label=label))
    return notes


def _transactions(session: Session, player_id: int, day: date) -> list[Annotation]:
    found = session.execute(
        select(Transaction.date, Transaction.type, Transaction.detail)
        .where(Transaction.player_id == player_id, Transaction.date <= day)
        .order_by(Transaction.date, Transaction.id)
    ).all()
    return [
        Annotation(game_date=when, kind="transaction", label=detail or kind)
        for when, kind, detail in found
    ]


def _tier_events(session: Session, player_id: int, since: date, day: date) -> list[Annotation]:
    """A note per rotation event of his from `since` through `day`: "Fringe → Core", or "Joined as
    Core" for an arrival."""
    found = session.execute(
        select(RotationEvent.game_date, RotationEvent.from_tier, RotationEvent.to_tier)
        .where(
            RotationEvent.player_id == player_id,
            RotationEvent.game_date >= since,
            RotationEvent.game_date <= day,
        )
        .order_by(RotationEvent.game_date, RotationEvent.team_id)
    ).all()
    return [
        Annotation(
            game_date=when,
            kind="tier",
            label=f"Joined as {to_tier.capitalize()}"
            if from_tier is None
            else f"{from_tier.capitalize()} → {to_tier.capitalize()}",
        )
        for when, from_tier, to_tier in found
    ]


def _closing(stints: Sequence[_Stint]) -> list[_Stint]:
    """The game's closing stints: those of the final `CLOSING_SECONDS` of its last period that
    start with the margin, the team's points for less against over its earlier stints, closer than
    `CLOSE_MARGIN`."""
    if not stints:
        return []
    last = max(stint.period for stint in stints)
    length = 720 if last <= 4 else 300
    margin = 0
    closing: list[_Stint] = []
    for stint in stints:
        if (
            stint.period == last
            and stint.end_sec > length - CLOSING_SECONDS
            and abs(margin) < CLOSE_MARGIN
        ):
            closing.append(stint)
        margin += stint.row.pts_for - stint.row.pts_against
    return closing


def _roles(
    player_id: int,
    games: Sequence[_Game],
    lines: dict[str, _Line],
    stints: dict[str, list[_Stint]],
) -> list[Annotation]:
    """A note at each game he played whose start, or whose close of a closing situation, differs
    from the previous such game's."""
    notes: list[Annotation] = []
    started: bool | None = None
    closed: bool | None = None
    for game in games:
        line = lines.get(game.game_id)
        if line is None or line.seconds <= 0:
            continue
        if started is not None and line.is_starter != started:
            label = "Started" if line.is_starter else "Came off the bench"
            notes.append(Annotation(game_date=game.game_date, kind="starter", label=label))
        started = line.is_starter
        closing = _closing(stints.get(game.game_id, []))
        if not closing:
            continue
        on = any(player_id in stint.members for stint in closing)
        if closed is not None and on != closed:
            label = "Closed" if on else "Did not close"
            notes.append(Annotation(game_date=game.game_date, kind="closer", label=label))
        closed = on
    return notes


# Shot profile.


def _shot_profile(
    session: Session, player_id: int, latest: date | None, window: Window, day: date
) -> list[ShotZone]:
    """Each zone over the window, the season and the prior season, from his production rows.
    Null where he has no row or no charted attempts."""
    columns = [f"{zone}_{part}" for zone in SHOT_ZONES for part in ("freq", "fg_pct")]
    rows = _production_rows(
        session, player_id, latest, day, [window, "season", "prior_season"], columns
    )

    def value(name: str, column: str) -> float | None:
        row = rows.get(name)
        return None if row is None else row[column]

    return [
        ShotZone(
            zone=cast(Any, zone),
            freq=value(window, f"{zone}_freq"),
            fg_pct=value(window, f"{zone}_fg_pct"),
            season_freq=value("season", f"{zone}_freq"),
            season_fg_pct=value("season", f"{zone}_fg_pct"),
            prior_freq=value("prior_season", f"{zone}_freq"),
            prior_fg_pct=value("prior_season", f"{zone}_fg_pct"),
        )
        for zone in SHOT_ZONES
    ]


def _production_rows(
    session: Session,
    player_id: int,
    latest: date | None,
    day: date,
    windows: list[str],
    columns: list[str],
) -> dict[str, dict[str, Any]]:
    """His production rows' `columns` in `windows` on `latest`, by window; before his first game of
    `day`'s season, the prior season's alone, his season window at his last game of it."""
    if latest is not None:
        return _production(session, player_id, latest, windows, columns)
    start, _ = season_bounds(season_label(day))
    before = session.scalar(
        text(
            "SELECT max(game_date) FROM player_window_stats "
            "WHERE player_id = :player AND team_id IS NULL AND game_date < :start"
        ),
        {"player": player_id, "start": start},
    )
    if before is None or season_label(before) != season_label(start - timedelta(days=1)):
        return {}
    prior = _production(session, player_id, before, ["season"], columns).get("season")
    return {} if prior is None else {"prior_season": prior}


def _production(
    session: Session, player_id: int, day: date, windows: list[str], columns: list[str]
) -> dict[str, dict[str, Any]]:
    """His production rows' `columns` on `day`, by window."""
    found = session.execute(
        text(
            f'SELECT "window", {", ".join(columns)} FROM player_window_stats '
            "WHERE player_id = :player AND team_id IS NULL AND game_date = :day "
            'AND "window" = ANY(:windows)'
        ),
        {"player": player_id, "day": day, "windows": windows},
    ).mappings()
    return {row["window"]: dict(row) for row in found}


# Production table.


class _Metric(NamedTuple):
    metric: ProductionMetric
    label: str
    format: ProductionFormat
    column: str
    windows: tuple[str, ...] = PRODUCTION_WINDOWS
    # Whether it has a delta against the season window.
    delta: bool = True


# v2 spec §8.6's order.
PRODUCTION_METRICS = (
    _Metric("ts_pct", "TS%", "percent", "ts_pct"),
    _Metric("efg_pct", "eFG%", "percent", "efg_pct"),
    _Metric("usg_pct", "USG%", "percent", "usg_pct"),
    _Metric("ast_pct", "AST%", "percent", "ast_pct"),
    _Metric("tov_pct", "TOV%", "percent", "tov_pct"),
    _Metric("oreb_pct", "OREB%", "percent", "oreb_pct"),
    _Metric("dreb_pct", "DREB%", "percent", "dreb_pct"),
    _Metric("ftr", "FTr", "rate", "ftr"),
    _Metric("fg3a_rate", "3PAr", "rate", "fg3a_rate"),
    _Metric("pts_per36", "PTS/36", "number", "pts_per36"),
    _Metric("reb_per36", "REB/36", "number", "reb_per36"),
    _Metric("ast_per36", "AST/36", "number", "ast_per36"),
    _Metric("game_score", "Game Score", "per_game", "game_score"),
    _Metric("ws", "Win Shares", "number", "ws_window", delta=False),
    _Metric("ws_48", "WS/48", "rate", "ws_48"),
    _Metric("vorp", "VORP", "number", "vorp_local", SEASON_WINDOWS, delta=False),
)


def _production_table(
    session: Session, player_id: int, latest: date | None, day: date
) -> list[ProductionRow]:
    """Each metric over his production windows, from the rows the shot profile reads. Win Shares
    carry no delta: a window's are a total over its games and the season's over the season to
    date, so their difference measures the window's length, not a change in him; WS/48, their
    rate, does carry one. VORP is kept for the season and the prior season only, with no delta for
    the same reason. Before his first game of `day`'s season the prior season has no season to
    compare with, and its Win Shares and VORP are that season's final snapshots, as the view's
    prior_season rows read them."""
    columns = [
        "games_in_window",
        "is_short",
        "ws_window_o",
        "ws_window_d",
        *(metric.column for metric in PRODUCTION_METRICS),
        *(f"{metric.column}_delta_season" for metric in PRODUCTION_METRICS if metric.delta),
    ]
    rows = _production_rows(session, player_id, latest, day, list(PRODUCTION_WINDOWS), columns)
    prior = rows.get("prior_season")
    if latest is None and prior is not None:
        prior.update({column: None for column in columns if column.endswith("_delta_season")})
        prior.update(_final_values(session, player_id, day))
    return [
        ProductionRow(
            metric=metric.metric,
            label=metric.label,
            format=metric.format,
            windows=ProductionWindows.model_validate(
                {
                    window: _cell(metric, rows.get(window)) if window in metric.windows else None
                    for window in PRODUCTION_WINDOWS
                }
            ),
        )
        for metric in PRODUCTION_METRICS
    ]


def _cell(metric: _Metric, row: dict[str, Any] | None) -> ProductionCell | None:
    if row is None:
        return None
    ws = metric.metric == "ws"
    return ProductionCell(
        value=row[metric.column],
        delta_season=row.get(f"{metric.column}_delta_season"),
        games=row["games_in_window"],
        is_short=row["is_short"],
        o_value=row["ws_window_o"] if ws else None,
        d_value=row["ws_window_d"] if ws else None,
    )


def _final_values(session: Session, player_id: int, day: date) -> dict[str, float | None]:
    """His Win Shares, with its parts, and VORP at the end of the season before `day`'s: each
    source's latest snapshot dated in it."""
    start, _ = season_bounds(season_label(day))
    prior_start, _ = season_bounds(season_label(start - timedelta(days=1)))
    found = session.execute(
        select(
            ImpactSnapshot.source,
            ImpactSnapshot.value,
            ImpactSnapshot.o_value,
            ImpactSnapshot.d_value,
        )
        .where(
            ImpactSnapshot.player_id == player_id,
            ImpactSnapshot.source.in_(VALUE_METRICS),
            ImpactSnapshot.date >= prior_start,
            ImpactSnapshot.date < start,
        )
        .order_by(ImpactSnapshot.date)
    ).all()
    latest = {source: (value, o_value, d_value) for source, value, o_value, d_value in found}
    ws, ws_o, ws_d = latest.get("ws_local", (None, None, None))
    vorp, _, _ = latest.get("vorp_local", (None, None, None))
    return {"ws_window": ws, "ws_window_o": ws_o, "ws_window_d": ws_d, "vorp_local": vorp}


# Teammates and matchups.


def _teammates(
    player_id: int,
    shown: Sequence[_Game],
    stints: dict[str, list[_Stint]],
    garbage_time: GarbageTime,
) -> list[tuple[int, Totals]]:
    """The `TOP_TEAMMATES` teammates with the most offensive possessions on the floor with him,
    then by player ID, with their totals together."""
    shared: dict[int, Totals] = defaultdict(Totals)
    for game in shown:
        for stint in stints.get(game.game_id, []):
            if stint.counts(garbage_time) and player_id in stint.members:
                for mate in stint.members - {player_id}:
                    shared[mate].add(stint.row)
    ranked = sorted(shared.items(), key=lambda item: (-item[1].possessions, item[0]))
    return ranked[:TOP_TEAMMATES]


class _Matched(NamedTuple):
    player_id: int
    partial_poss: float
    pts: int
    fgm: int
    fga: int
    fg3m: int
    fg3a: int
    tov: int
    ast: int

    def model(self, names: dict[int, str]) -> MatchupRow:
        return MatchupRow(
            player=player_ref(self.player_id, names),
            partial_poss=self.partial_poss,
            pts=self.pts,
            fgm=self.fgm,
            fga=self.fga,
            fg3m=self.fg3m,
            fg3a=self.fg3a,
            tov=self.tov,
            ast=self.ast,
        )


def _matchups(
    session: Session, player_id: int, game_ids: list[str]
) -> tuple[list[_Matched], list[_Matched]]:
    """The defenders who guarded him and the players he guarded over `game_ids`, each the
    `TOP_MATCHUPS` with the most partial possessions, then by player ID."""
    if not game_ids:
        return [], []
    sides: list[list[_Matched]] = []
    for mine, theirs in (
        (Matchup.off_player_id, Matchup.def_player_id),
        (Matchup.def_player_id, Matchup.off_player_id),
    ):
        possessions = func.sum(Matchup.partial_poss)
        found = session.execute(
            select(
                theirs,
                possessions,
                *(
                    func.sum(column)
                    for column in (
                        Matchup.pts,
                        Matchup.fgm,
                        Matchup.fga,
                        Matchup.fg3m,
                        Matchup.fg3a,
                        Matchup.tov,
                        Matchup.ast,
                    )
                ),
            )
            .where(mine == player_id, Matchup.game_id.in_(game_ids))
            .group_by(theirs)
            .order_by(possessions.desc(), theirs)
            .limit(TOP_MATCHUPS)
        ).all()
        sides.append([_Matched(*row) for row in found])
    return sides[0], sides[1]


# Flags.


def _flags(session: Session, player_id: int, day: date) -> list[PlayerFlag]:
    start, _ = season_bounds(season_label(day))
    return list(
        session.scalars(
            select(PlayerFlag)
            .where(
                PlayerFlag.player_id == player_id,
                PlayerFlag.game_date >= start,
                PlayerFlag.game_date <= day,
            )
            .order_by(PlayerFlag.game_date.desc(), PlayerFlag.flag)
        )
    )
