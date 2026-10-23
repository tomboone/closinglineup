import json
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from closinglineup import injuries
from closinglineup.clock import FakeClock
from closinglineup.db.models import (
    Game,
    InjuryStatus,
    PlayerGame,
    RosterMembership,
    RunStatus,
    Transaction,
)
from closinglineup.derive.roster import (
    ON_ASSIGNMENT,
    current_g_league_assignment,
    merge_memberships,
    rebuild_roster_membership,
)
from closinglineup.names import NameIndex, load_aliases
from closinglineup.parse.rows import RosterMembershipRow
from closinglineup.sources.cache import RawCache
from closinglineup.sources.injury_pdf import EASTERN
from closinglineup.sources.injury_pdf import parse as parse_report
from closinglineup.sources.transactions import TransactionRow, TransactionType, parse
from closinglineup.teams import by_tricode
from tests.test_injuries import PLAYERS as REPORT_PLAYERS
from tests.test_injuries import REPORTED
from tests.test_injury_pdf import WEEKEND
from tests.test_transactions import FEED

GSW, DEN, LAL = (by_tricode(t).nba_team_id for t in ("GSW", "DEN", "LAL"))
MEM, CHI, CHA, HOU = (by_tricode(t).nba_team_id for t in ("MEM", "CHI", "CHA", "HOU"))
# Player IDs no real player has, so leftovers of other tests on the same database stay out.
PLAYER, OTHER, TEAMMATE = 9_000_001, 9_000_002, 9_000_003
PLAYERS = (PLAYER, OTHER, TEAMMATE)
DAY = date(2019, 10, 23)

# deploy/README.md's coverage check: player_game rows of the season with no covering membership.
UNCOVERED = text(
    """
    SELECT count(*) FROM player_game pg JOIN game g USING (game_id)
    WHERE g.season = :season AND NOT EXISTS (
        SELECT 1 FROM roster_membership rm
        WHERE rm.player_id = pg.player_id AND rm.team_id = pg.team_id
          AND rm.start_date <= g.game_date
          AND (rm.end_date IS NULL OR rm.end_date >= g.game_date)
    )
    """
)

type Span = tuple[int, date, date | None, str, str]


def _day(offset: int) -> date:
    return DAY + timedelta(days=offset)


def _move(
    offset: int,
    kind: TransactionType,
    *,
    to: int | None = None,
    frm: int | None = None,
    detail: str = "",
    player_id: int = PLAYER,
) -> TransactionRow:
    return TransactionRow(_day(offset), kind, player_id, frm, to, detail)


def _roster(offset: int, team_id: int, player_id: int = PLAYER, **kw: str) -> RosterMembershipRow:
    return RosterMembershipRow.model_validate(
        dict(
            player_id=player_id,
            team_id=team_id,
            start_date=_day(offset),
            source="commonteamroster",
            **kw,
        )
    )


def _spans(rows: list[RosterMembershipRow], player_id: int = PLAYER) -> list[Span]:
    return [
        (r.team_id, r.start_date, r.end_date, r.contract_type, r.source)
        for r in rows
        if r.player_id == player_id
    ]


def _overlapping(rows: list[RosterMembershipRow]) -> list[int]:
    by_player: defaultdict[int, list[RosterMembershipRow]] = defaultdict(list)
    for row in rows:
        by_player[row.player_id].append(row)
    found: list[int] = []
    for player_id, spans in by_player.items():
        spans.sort(key=lambda r: r.start_date)
        for before, after in pairwise(spans):
            if before.end_date is None or before.end_date >= after.start_date:
                found.append(player_id)
                break
        if any(r.end_date is not None and r.end_date < r.start_date for r in spans):
            found.append(player_id)
    return found


def test_the_fixture_feed_gives_no_player_overlapping_spans_and_a_waiver_ends_a_span_that_day() -> (
    None
):
    rows = merge_memberships(parse(json.loads(FEED.read_text())).rows, [], [])

    assert rows
    assert _overlapping(rows) == []
    # Jordan Hawkins: traded to Memphis, waived by it, then signed by Chicago on a two-way.
    assert _spans(rows, 1641722) == [
        (MEM, date(2026, 9, 8), date(2026, 10, 2), "standard", "transaction"),
        (CHI, date(2026, 10, 5), None, "two_way", "transaction"),
    ]
    # Buddy Hield: traded twice; the first trade's span ends the day before the second.
    assert _spans(rows, 1627741) == [
        (CHA, date(2026, 9, 22), date(2026, 9, 26), "standard", "transaction"),
        (CHI, date(2026, 9, 27), None, "standard", "transaction"),
    ]
    # Rafael Castro: a two-way with Houston until it waived him. Waived by the Lakers too, whose
    # signing the feed does not hold: a waiver opens nothing.
    assert _spans(rows, 1643572) == [
        (HOU, date(2026, 8, 26), date(2026, 10, 1), "two_way", "transaction"),
    ]


def test_games_alone_give_a_span_per_stint_each_ending_at_its_last_game() -> None:
    games = [(PLAYER, GSW, _day(0)), (PLAYER, GSW, _day(2)), (PLAYER, DEN, _day(10))]
    games += [(PLAYER, GSW, _day(17)), (OTHER, LAL, _day(0)), (OTHER, LAL, _day(17))]

    rows = merge_memberships([], games, [])

    assert _spans(rows) == [
        (GSW, _day(0), _day(2), "standard", "player_game"),
        (DEN, _day(10), _day(10), "standard", "player_game"),
        (GSW, _day(17), None, "standard", "player_game"),
    ]
    assert _spans(rows, OTHER) == [(LAL, _day(0), None, "standard", "player_game")]


def test_a_signing_and_waiver_on_one_day_is_a_span_of_that_day() -> None:
    rows = merge_memberships(
        [
            _move(0, "sign", to=GSW, detail="Golden State Warriors signed X to a Contract."),
            _move(0, "waive", frm=GSW),
        ],
        [],
        [],
    )

    assert _spans(rows) == [(GSW, _day(0), _day(0), "standard", "transaction")]


def test_a_player_claimed_off_waivers_is_on_no_team_while_on_them() -> None:
    rows = merge_memberships(
        [
            _move(0, "sign", to=GSW),
            _move(5, "waive", frm=GSW),
            _move(7, "sign", to=DEN, detail="Denver Nuggets claimed X off waivers."),
        ],
        [],
        [],
    )

    assert _spans(rows) == [
        (GSW, _day(0), _day(5), "standard", "transaction"),
        (DEN, _day(7), None, "standard", "transaction"),
    ]


def test_a_new_contract_on_the_same_team_splits_the_span_and_a_traded_player_keeps_his() -> None:
    rows = merge_memberships(
        [
            _move(0, "two_way_sign", to=GSW, detail="GSW signed X to a Two-Way Contract."),
            # Another two-way signing with the same team runs the span on.
            _move(3, "sign", to=GSW, detail="GSW signed X to a Two-Way Contract."),
            _move(6, "trade", frm=GSW, to=DEN),
            _move(9, "two_way_convert", to=DEN, detail="DEN converted X to an NBA Contract."),
        ],
        [(PLAYER, GSW, _day(1)), (PLAYER, DEN, _day(7))],
        [],
    )

    assert _spans(rows) == [
        (GSW, _day(0), _day(5), "two_way", "transaction"),
        (DEN, _day(6), _day(8), "two_way", "transaction"),
        (DEN, _day(9), None, "standard", "transaction"),
    ]


def test_a_later_roster_that_does_not_list_a_player_ends_his_span_at_his_last_game() -> None:
    rows = merge_memberships(
        [_move(0, "sign", to=GSW)],
        [(PLAYER, GSW, _day(2)), (OTHER, GSW, _day(12))],
        # GSW's roster of day 12 lists OTHER, who joined that day, and not PLAYER.
        [_roster(12, GSW, OTHER, contract_type="ten_day")],
    )

    assert _spans(rows) == [(GSW, _day(0), _day(2), "standard", "transaction")]
    assert _spans(rows, OTHER) == [(GSW, _day(12), None, "ten_day", "commonteamroster")]


def test_a_roster_line_opens_a_span_and_a_later_game_for_the_team_runs_it_on() -> None:
    rows = merge_memberships([], [(PLAYER, GSW, _day(4))], [_roster(0, GSW)])

    assert _spans(rows) == [(GSW, _day(0), None, "standard", "commonteamroster")]


def test_a_player_who_left_the_league_ends_at_his_last_game_not_at_the_next_roster() -> None:
    # Last played for GSW on day 2; GSW's roster of the next season, day 300, no longer lists him.
    rows = merge_memberships(
        [],
        [(PLAYER, GSW, _day(0)), (PLAYER, GSW, _day(2)), (OTHER, GSW, _day(300))],
        [_roster(300, GSW, OTHER)],
    )

    assert _spans(rows) == [(GSW, _day(0), _day(2), "standard", "player_game")]


def test_a_free_agent_signing_elsewhere_ends_at_his_last_game_a_trade_the_day_before() -> None:
    rows = merge_memberships(
        [_move(40, "sign", to=DEN), _move(60, "trade", frm=DEN, to=LAL)],
        [(PLAYER, GSW, _day(0)), (PLAYER, GSW, _day(2)), (PLAYER, DEN, _day(45))],
        [],
    )

    assert _spans(rows) == [
        (GSW, _day(0), _day(2), "standard", "player_game"),
        (DEN, _day(40), _day(59), "standard", "transaction"),
        (LAL, _day(60), None, "standard", "transaction"),
    ]


def test_a_move_dated_the_day_he_played_for_his_old_team_starts_the_next_day() -> None:
    rows = merge_memberships(
        [_move(5, "trade", frm=GSW, to=DEN)], [(PLAYER, GSW, _day(0)), (PLAYER, GSW, _day(5))], []
    )

    assert _spans(rows) == [
        (GSW, _day(0), _day(5), "standard", "player_game"),
        (DEN, _day(6), None, "standard", "transaction"),
    ]


def test_a_two_way_player_missing_from_a_lagging_roster_keeps_his_contract() -> None:
    rows = merge_memberships(
        [_move(0, "two_way_sign", to=GSW, detail="GSW signed X to a Two-Way Contract.")],
        [(PLAYER, GSW, _day(2)), (PLAYER, GSW, _day(12)), (OTHER, GSW, _day(10))],
        [_roster(10, GSW, OTHER)],
    )

    assert _spans(rows) == [
        (GSW, _day(0), _day(2), "two_way", "transaction"),
        (GSW, _day(12), None, "two_way", "player_game"),
    ]


# The database-backed rebuild, over a season no other test loads.

SEASON = "2019-20"
# (game ID, date): PLAYER plays the first two for GSW, the third for DEN, the fourth for GSW.
GAMES = [
    ("0021900901", _day(0)),
    ("0021900902", _day(2)),
    ("0021900903", _day(10)),
    ("0021900904", _day(17)),
]
TEAMS_OF_PLAYER = [GSW, GSW, DEN, GSW]


def _player_game(game_id: str, player_id: int, team_id: int, seconds: int) -> PlayerGame:
    counts = dict.fromkeys(
        ("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl", "blk"),
        0,
    )
    return PlayerGame(
        game_id=game_id,
        player_id=player_id,
        team_id=team_id,
        is_starter=False,
        seconds=seconds,
        # A DNP is still a game with the team.
        dnp_reason=None if seconds else "cd",
        tov=0,
        pf=0,
        plus_minus=0,
        source="stats",
        **counts,
    )


def _clear(db: sessionmaker[Session]) -> None:
    game_ids = [game_id for game_id, _ in GAMES]
    with db.begin() as session:
        session.execute(delete(PlayerGame).where(PlayerGame.game_id.in_(game_ids)))
        session.execute(delete(Game).where(Game.game_id.in_(game_ids)))
        session.execute(delete(RosterMembership).where(RosterMembership.player_id.in_(PLAYERS)))
        session.execute(delete(Transaction).where(Transaction.player_id.in_(PLAYERS)))
        session.execute(delete(InjuryStatus).where(InjuryStatus.player_id.in_(PLAYERS)))


@pytest.fixture
def season(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """`GAMES` loaded: PLAYER in each, OTHER for LAL in the first and, as a DNP, the last; OTHER
    waived by LAL the day after the last."""
    _clear(db)
    with db.begin() as session:
        for game_id, day in GAMES:
            session.add(
                Game(
                    game_id=game_id,
                    season=SEASON,
                    game_date=day,
                    phase="regular",
                    home_team_id=GSW,
                    away_team_id=DEN,
                    status="final",
                    home_b2b=False,
                    away_b2b=False,
                )
            )
        session.flush()
        for (game_id, _), team_id in zip(GAMES, TEAMS_OF_PLAYER, strict=True):
            session.add(_player_game(game_id, PLAYER, team_id, 600))
        session.add(_player_game(GAMES[0][0], OTHER, LAL, 1200))
        session.add(_player_game(GAMES[-1][0], OTHER, LAL, 0))
        session.add(
            Transaction(
                date=_day(18),
                type="waive",
                player_id=OTHER,
                from_team_id=LAL,
                detail="Los Angeles Lakers waived X.",
            )
        )
    yield db
    _clear(db)


def _stored(db: sessionmaker[Session]) -> list[tuple[int, int, date, date | None, str]]:
    with db() as session:
        found = session.execute(
            select(
                RosterMembership.player_id,
                RosterMembership.team_id,
                RosterMembership.start_date,
                RosterMembership.end_date,
                RosterMembership.source,
            )
            .where(RosterMembership.player_id.in_(PLAYERS))
            .order_by(RosterMembership.player_id, RosterMembership.start_date)
        )
        return [(p, t, s, e, src) for p, t, s, e, src in found]


def _uncovered(db: sessionmaker[Session]) -> int:
    with db() as session:
        return session.execute(UNCOVERED, {"season": SEASON}).scalar_one()


def test_the_rebuild_covers_every_game_replaces_stale_spans_and_a_rerun_changes_nothing(
    season: sessionmaker[Session], tmp_path: Path
) -> None:
    stale = RosterMembership(
        player_id=PLAYER,
        team_id=LAL,
        start_date=date(2019, 3, 1),
        contract_type="standard",
        source="commonteamroster",
    )
    with season.begin() as session:
        session.add(stale)
    cache = RawCache(tmp_path, FakeClock(datetime(2019, 11, 20, tzinfo=UTC)))
    assert _uncovered(season) == 6

    first = rebuild_roster_membership(season, cache, UTC)
    second = rebuild_roster_membership(season, cache, UTC)

    assert (first.status, first.key) == (RunStatus.SUCCEEDED, "all")
    assert first.removed >= 1
    assert (second.changed, second.removed) == (0, 0)
    assert _uncovered(season) == 0
    assert _stored(season) == [
        (PLAYER, GSW, _day(0), _day(2), "player_game"),
        (PLAYER, DEN, _day(10), _day(10), "player_game"),
        (PLAYER, GSW, _day(17), None, "player_game"),
        (OTHER, LAL, _day(0), _day(18), "player_game"),
    ]


# G League assignment, read from the official injury report (spec §7.5).

TWO_WAY = "G League - Two-Way"


@pytest.fixture
def reports(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """No injury rows or transactions of the test players, before and after."""
    _clear(db)
    yield db
    _clear(db)


def _at(offset: int, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(_day(offset), time(hour, minute), EASTERN)


def _report(
    db: sessionmaker[Session],
    at: datetime,
    *lines: tuple[int, str | None],
    team_id: int = GSW,
    game_date: date | None = None,
    status: str = "Out",
    source: str = "pdf",
) -> None:
    """One report's lines for the team, each (player ID, reason), for the game on `game_date`,
    the report's date by default."""
    with db.begin() as session:
        session.execute(
            insert(InjuryStatus),
            [
                {
                    "report_datetime": at,
                    "game_date": game_date or at.date(),
                    "player_id": player_id,
                    "team_id": team_id,
                    "status": status,
                    "reason": reason,
                    "source": source,
                }
                for player_id, reason in lines
            ],
        )


def _transaction(offset: int, kind: str) -> Transaction:
    return Transaction(
        date=_day(offset),
        type=kind,
        player_id=PLAYER,
        from_team_id=GSW if kind == "assign" else None,
        to_team_id=GSW if kind == "recall" else None,
        detail=f"{kind} {offset}",
    )


def _assigned(db: sessionmaker[Session], *offsets: int, player_id: int = PLAYER) -> list[bool]:
    with db() as session:
        return [current_g_league_assignment(session, player_id, _day(o)) for o in offsets]


def test_every_on_assignment_line_of_the_fixture_report_reads_assigned(
    db: sessionmaker[Session],
) -> None:
    index = NameIndex(REPORT_PLAYERS, load_aliases())
    rows: list[dict[str, Any]] = injuries.resolve_rows(parse_report(WEEKEND), index, {}, {}).rows
    by_reason: defaultdict[str | None, list[int]] = defaultdict(list)
    for row in rows:
        by_reason[row["reason"]].append(row["player_id"])
    assigned, two_way = by_reason[ON_ASSIGNMENT], by_reason[TWO_WAY]
    injured = [
        player_id
        for reason, players in by_reason.items()
        if reason and reason.startswith("Injury/Illness")
        for player_id in players
    ]
    # The Questionable and the Available line are among the eight.
    assert len(assigned) == 8 and two_way and injured
    clear = delete(InjuryStatus).where(
        InjuryStatus.report_datetime == REPORTED, InjuryStatus.source == "pdf"
    )
    with db.begin() as session:
        session.execute(clear)
        session.execute(insert(InjuryStatus), rows)
    try:
        with db() as session:
            read = {
                player_id: current_g_league_assignment(session, player_id, REPORTED.date())
                for player_id in assigned + two_way + injured
            }
    finally:
        with db.begin() as session:
            session.execute(clear)

    assert all(read[player_id] for player_id in assigned)
    assert not any(read[player_id] for player_id in two_way + injured)


def test_an_assignment_holds_until_a_newer_report_for_the_team_omits_him(
    reports: sessionmaker[Session],
) -> None:
    _report(reports, _at(1, 17), (PLAYER, ON_ASSIGNMENT), (TEAMMATE, TWO_WAY))
    _report(reports, _at(3, 17), (TEAMMATE, TWO_WAY))

    # Day 0 before the assignment, day 1 assigned, day 2 (no game) still, day 3 recalled, day 4
    # still.
    assert _assigned(reports, 0, 1, 2, 3, 4) == [False, True, True, False, False]
    assert _assigned(reports, 1, 3, player_id=TEAMMATE) == [False, False]


def test_a_later_edition_of_the_same_games_report_that_drops_him_reads_recalled(
    reports: sessionmaker[Session],
) -> None:
    _report(reports, _at(1, 13), (PLAYER, ON_ASSIGNMENT))
    _report(reports, _at(1, 17, 15), (TEAMMATE, "Ankle"))

    assert _assigned(reports, 1, 2) == [False, False]


def test_a_newer_report_for_another_team_is_not_a_recall(reports: sessionmaker[Session]) -> None:
    _report(reports, _at(1, 17), (PLAYER, ON_ASSIGNMENT))
    _report(reports, _at(2, 17), (TEAMMATE, "Ankle"), team_id=DEN)

    assert _assigned(reports, 2) == [True]


def test_a_line_for_tomorrows_game_is_read_tomorrow_not_today(
    reports: sessionmaker[Session],
) -> None:
    _report(reports, _at(0, 17), (PLAYER, ON_ASSIGNMENT), game_date=_day(1))

    assert _assigned(reports, 0, 1) == [False, True]


def test_espn_rows_are_ignored_whatever_their_age(reports: sessionmaker[Session]) -> None:
    _report(reports, _at(1, 17), (PLAYER, ON_ASSIGNMENT))
    _report(reports, _at(2, 12), (TEAMMATE, "Ankle"), source="espn")
    _report(reports, _at(3, 17), (TEAMMATE, TWO_WAY))
    _report(reports, _at(4, 12), (PLAYER, "Knee"), source="espn")

    # Day 2: ESPN's newer row does not recall him. Day 4: nor does it assign him.
    assert _assigned(reports, 2, 3, 4) == [True, False, False]


def test_a_transaction_dated_on_or_after_the_last_report_line_overrides_it(
    reports: sessionmaker[Session],
) -> None:
    _report(reports, _at(1, 17), (PLAYER, "Knee"))
    _report(reports, _at(5, 17), (PLAYER, ON_ASSIGNMENT))
    with reports.begin() as session:
        session.add_all([_transaction(2, "assign"), _transaction(5, "recall")])

    # Day 1: the report alone. Day 2: an assignment after the line. Day 5: a recall dated the
    # line's day wins the tie.
    assert _assigned(reports, 1, 2, 4, 5) == [False, True, True, False]


def test_a_transaction_dated_before_the_last_report_line_does_not_override_it(
    reports: sessionmaker[Session],
) -> None:
    _report(reports, _at(1, 17), (PLAYER, "Knee"))
    _report(reports, _at(4, 17), (PLAYER, ON_ASSIGNMENT))
    with reports.begin() as session:
        session.add_all([_transaction(0, "assign"), _transaction(3, "recall")])

    assert _assigned(reports, 0, 1, 3, 4) == [True, False, False, True]
