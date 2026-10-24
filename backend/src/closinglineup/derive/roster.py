"""`roster_membership`, rebuilt whole from rosters, transactions and games (spec §6.1), and a
player's G League assignment (spec §7.5).

`merge_memberships` reads three kinds of evidence of a player on a team, per player in date order:

- a transaction: a signing, claim or conversion (`sign`, `two_way_sign`, `two_way_convert`), a
  trade, or a waiver;
- a game: a `player_game` row for the team, a DNP included;
- a roster: his line on a cached team roster, dated the day it was fetched.

A span opens at the first evidence of the player on a team and runs until the evidence that ends
it. A waiver ends it on its own date, and a trade away the day before. Evidence of him on another
team, or a later roster of the team that does not list him, ends it at the last evidence of him on
the team, so a player who leaves the league, or whose contract runs out, is not left on his old team
until he turns up again: his last game, say. A signing or conversion to a different contract on the
same team ends it the day before, and opens the next span with the new contract. A span nothing
ends is open (`end_date` NULL). So a player's spans never overlap: one that would start on the day
the last one ended moves that end back a day, and a span left with no days is dropped. A move dated
the day the player played for his old team starts the next day, so the game stays covered.

Within a day the evidence is read in this order: a roster (fetched at 09:00), a game, then the
day's trades, signings and waivers, so a player signed and waived on one day, as an Exhibit 10
often is, has a span of that day. A waived player is not signed or claimed the same day: he is on
waivers for 48 hours first.

A span's contract type comes from the transaction that opened it (`parse.roster.contract_type` of
its description: two-way, 10-day, Exhibit 10, or `standard`), and a traded player keeps his. A span
a game or roster opened has what the roster's `HOW_ACQUIRED` names, else the contract of the
player's last span if that was on the same team, else `standard`. Its `source` is
what opened it: `transaction`, `player_game` or `commonteamroster`.

G League assignments and recalls do not move a player off his team. `current_g_league_assignment`
reads them from the official injury report (`injury_status`, `source='pdf'`), which lists a player
on assignment as `G League - On Assignment` whatever his status: his latest line on or before the
day says whether he is assigned, and a newer report with any line for his team that omits him means
he is recalled, since a report lists every unavailable player of each team whose game it covers.
ESPN's fallback rows are ignored, as their reasons are body parts, not roster status. NBA.com's
player-movement feed carries no `assign` / `recall` rows (`sources.transactions`); should one
appear, it decides from its date on, over a report line of that date or earlier.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from enum import IntEnum
from typing import NamedTuple, cast

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import Base, Game, InjuryStatus, PlayerGame, RunStatus, Transaction
from closinglineup.db.replace import replace_rows
from closinglineup.injuries import PDF_SOURCE
from closinglineup.load.loader import NATURAL_KEYS, LoadResult
from closinglineup.parse.roster import PARTICIPATION_SOURCE, ROSTER_SOURCE, contract_type
from closinglineup.parse.rows import ContractType, RosterMembershipRow
from closinglineup.pipeline import PAYLOAD_ERRORS, roster_snapshots
from closinglineup.sources.cache import RawCache
from closinglineup.sources.injury_pdf import EASTERN
from closinglineup.sources.transactions import TransactionRow, TransactionType

TABLE = "roster_membership"
TRANSACTION_SOURCE = "transaction"
# `LoadResult.key` of a rebuild, which covers every season.
ALL_SEASONS = "all"
ONE_DAY = timedelta(days=1)

# The transaction types that put a player on `to_team_id` with the contract its description names.
JOIN_TYPES = frozenset({"sign", "two_way_sign", "two_way_convert"})
ASSIGN, RECALL = "assign", "recall"
# The injury report's reason for a player on G League assignment, as the parser stores it.
ON_ASSIGNMENT = "G League - On Assignment"


class _Kind(IntEnum):
    """What a fact says, in the order a day's facts are read."""

    UNLISTED = 0
    ROSTER = 1
    GAME = 2
    TRADE = 3
    JOIN = 4
    WAIVE = 5


class _Fact(NamedTuple):
    day: date
    kind: _Kind
    team_id: int
    # A join's contract, or a roster's; None for the other kinds.
    contract: ContractType | None = None
    # A trade's team the player leaves.
    from_team_id: int | None = None


@dataclass(slots=True)
class _Span:
    team_id: int
    start: date
    end: date | None
    contract: ContractType
    source: str
    # The latest evidence of the player on the team.
    last: date


def merge_memberships(
    transactions: Iterable[TransactionRow],
    appearances: Iterable[tuple[int, int, date]],
    rosters: Iterable[RosterMembershipRow],
) -> list[RosterMembershipRow]:
    """Every player's spans, from `transactions`, `appearances` as (player ID, team ID, game date)
    and `rosters`' memberships, each a roster line open from the day it was fetched; sorted by
    player and start."""
    facts: defaultdict[int, list[_Fact]] = defaultdict(list)
    for row in transactions:
        if row.type in JOIN_TYPES and row.to_team_id is not None:
            facts[row.player_id].append(
                _Fact(row.date, _Kind.JOIN, row.to_team_id, contract_type(row.detail))
            )
        elif row.type == "waive" and row.from_team_id is not None:
            facts[row.player_id].append(_Fact(row.date, _Kind.WAIVE, row.from_team_id))
        elif row.type == "trade" and row.to_team_id is not None:
            facts[row.player_id].append(
                _Fact(row.date, _Kind.TRADE, row.to_team_id, from_team_id=row.from_team_id)
            )
    for player_id, team_id, day in appearances:
        facts[player_id].append(_Fact(day, _Kind.GAME, team_id))
    listed: defaultdict[tuple[int, date], set[int]] = defaultdict(set)
    for line in rosters:
        facts[line.player_id].append(
            _Fact(line.start_date, _Kind.ROSTER, line.team_id, line.contract_type)
        )
        listed[(line.team_id, line.start_date)].add(line.player_id)
    for player_id, found in facts.items():
        teams = {fact.team_id for fact in found}
        found.extend(
            _Fact(as_of, _Kind.UNLISTED, team_id)
            for (team_id, as_of), players in listed.items()
            if team_id in teams and player_id not in players
        )
    return [
        RosterMembershipRow(
            player_id=player_id,
            team_id=span.team_id,
            start_date=span.start,
            end_date=span.end,
            contract_type=span.contract,
            source=span.source,
        )
        for player_id in sorted(facts)
        for span in _spans(sorted(facts[player_id], key=lambda fact: (fact.day, fact.kind)))
    ]


def _spans(facts: list[_Fact]) -> list[_Span]:
    spans: list[_Span] = []
    for fact in facts:
        current = spans[-1] if spans and spans[-1].end is None else None
        on_team = current is not None and current.team_id == fact.team_id
        match fact.kind:
            case _Kind.UNLISTED:
                if current is not None and on_team and current.start < fact.day:
                    current.end = current.last
            case _Kind.ROSTER | _Kind.GAME:
                if current is not None and on_team:
                    current.last = fact.day
                    continue
                source = PARTICIPATION_SOURCE if fact.kind is _Kind.GAME else ROSTER_SOURCE
                contract = _reopened_contract(spans, fact)
                _open(spans, fact.team_id, fact.day, contract, source, at_last=True)
            case _Kind.WAIVE:
                if current is not None and on_team:
                    current.end = fact.day
                    if current.end < current.start:
                        spans.pop()
            case _Kind.TRADE:
                if current is not None and on_team:
                    current.last = fact.day
                    continue
                leaving = current is not None and current.team_id == fact.from_team_id
                contract = current.contract if current is not None and leaving else "standard"
                _open(spans, fact.team_id, fact.day, contract, TRANSACTION_SOURCE, at_last=False)
            case _Kind.JOIN:
                contract = fact.contract or "standard"
                if current is not None and on_team:
                    if contract == current.contract:
                        current.last = fact.day
                        continue
                    if current.start == fact.day:
                        current.contract = contract
                        continue
                    _open(
                        spans, fact.team_id, fact.day, contract, TRANSACTION_SOURCE, at_last=False
                    )
                    continue
                _open(spans, fact.team_id, fact.day, contract, TRANSACTION_SOURCE, at_last=True)
    return spans


def _reopened_contract(spans: list[_Span], fact: _Fact) -> ContractType:
    """A game's or roster's span's contract: what the roster names, else the contract of the
    player's last span if it was on the same team, as a two-way player missing from a roster that
    lags has, else `standard`."""
    if fact.contract is not None and fact.contract != "standard":
        return fact.contract
    if spans and spans[-1].team_id == fact.team_id:
        return spans[-1].contract
    return "standard"


def _open(
    spans: list[_Span],
    team_id: int,
    day: date,
    contract: ContractType,
    source: str,
    *,
    at_last: bool,
) -> None:
    """Open a span on `day`, ending the last one if it is still open: at its last evidence if
    `at_last`, else the day before.

    A transaction that moves a player off a team he played for that day starts his new span the
    next day, so the game stays covered. The last span is then cut back to end before the new one
    starts, and dropped if that leaves it no days.
    """
    start = day
    if spans and spans[-1].end is None:
        last = spans[-1]
        if source == TRANSACTION_SOURCE and last.last == day and last.team_id != team_id:
            start, last.end = day + ONE_DAY, day
        else:
            last.end = min(last.last, day - ONE_DAY) if at_last else day - ONE_DAY
    if spans and spans[-1].end is not None and spans[-1].end >= start:
        spans[-1].end = start - ONE_DAY
    if spans and spans[-1].end is not None and spans[-1].end < spans[-1].start:
        spans.pop()
    spans.append(_Span(team_id, start, None, contract, source, start))


def rebuild_roster_membership(db: sessionmaker[Session], cache: RawCache, tz: tzinfo) -> LoadResult:
    """Replace every `roster_membership` row with `merge_memberships` of every transaction, every
    loaded game and every cached roster (`pipeline.roster_snapshots`, dated in `tz`), in one
    database transaction; a re-run with nothing new writes nothing.

    A database error, or a cached roster that does not parse, fails the rebuild rather than
    raising, as `load.loader.load_table` does with a load.
    """
    try:
        rosters = roster_snapshots(cache, tz)
        with db.begin() as session:
            spans = merge_memberships(_transactions(session), _appearances(session), rosters)
            changed, removed = replace_rows(
                session,
                Base.metadata.tables[TABLE],
                NATURAL_KEYS[TABLE],
                [span.model_dump() for span in spans],
            )
    except (SQLAlchemyError, *PAYLOAD_ERRORS) as exc:
        error = f"{type(exc).__name__}: {exc}"
        return LoadResult(TABLE, ALL_SEASONS, RunStatus.FAILED, error=error)
    return LoadResult(TABLE, ALL_SEASONS, RunStatus.SUCCEEDED, len(spans), changed, removed)


def current_g_league_assignment(session: Session, player_id: int, day: date) -> bool:
    """Whether the player is on G League assignment on `day`, from the official injury report: his
    latest `source='pdf'` line for a game on or before `day`, reported by the end of `day` in
    Eastern time, says `G League - On Assignment`, and no newer such report has a line for his
    team, which would list him if he were still unavailable. A day his team does not play carries
    the last report's state over. An `assign` / `recall` transaction dated on or after that line's
    Eastern date, or with no line, decides instead; an assignment and a recall on the same day
    read as recalled."""
    end = datetime.combine(day + ONE_DAY, time(), EASTERN)
    line = session.execute(
        select(InjuryStatus.team_id, InjuryStatus.report_datetime, InjuryStatus.reason)
        .where(
            InjuryStatus.player_id == player_id,
            InjuryStatus.source == PDF_SOURCE,
            InjuryStatus.game_date <= day,
            InjuryStatus.report_datetime < end,
        )
        .order_by(InjuryStatus.report_datetime.desc(), InjuryStatus.game_date.desc())
        .limit(1)
    ).first()
    assigned = False
    reported: date | None = None
    if line is not None:
        team_id, at, reason = line
        reported = at.astimezone(EASTERN).date()
        newer = session.scalar(
            select(InjuryStatus.id)
            .where(
                InjuryStatus.team_id == team_id,
                InjuryStatus.source == PDF_SOURCE,
                InjuryStatus.game_date <= day,
                InjuryStatus.report_datetime > at,
                InjuryStatus.report_datetime < end,
            )
            .limit(1)
        )
        assigned = reason == ON_ASSIGNMENT and newer is None
    move = session.execute(
        select(Transaction.date, Transaction.type)
        .where(
            Transaction.player_id == player_id,
            Transaction.type.in_((ASSIGN, RECALL)),
            Transaction.date <= day,
        )
        .order_by(Transaction.date.desc(), (Transaction.type == RECALL).desc())
        .limit(1)
    ).first()
    if move is not None and (reported is None or move.date >= reported):
        return move.type == ASSIGN
    return assigned


def _transactions(session: Session) -> list[TransactionRow]:
    found = session.scalars(select(Transaction))
    return [
        TransactionRow(
            row.date,
            cast(TransactionType, row.type),
            row.player_id,
            row.from_team_id,
            row.to_team_id,
            row.detail,
        )
        for row in found
    ]


def _appearances(session: Session) -> list[tuple[int, int, date]]:
    found = session.execute(
        select(PlayerGame.player_id, PlayerGame.team_id, Game.game_date).join(
            Game, Game.game_id == PlayerGame.game_id
        )
    )
    return [(player_id, team_id, game_date) for player_id, team_id, game_date in found]
