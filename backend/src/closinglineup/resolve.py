"""What the overlays read from the database to resolve a source's players: the name index over
`player` and the alias table (`closinglineup.names`), and each team's roster on a date, from
`roster_membership`, which tells apart two players of one name."""

from collections.abc import Collection, Mapping
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from closinglineup.db.models import Player, RosterMembership
from closinglineup.names import NameIndex, load_aliases

# (team ID, date) → the players on the team's roster that date.
type Rosters = Mapping[tuple[int, date], Collection[int]]


def name_index(session: Session) -> NameIndex:
    players = session.execute(select(Player.nba_player_id, Player.name))
    return NameIndex(players, load_aliases())


def known_players(session: Session) -> set[int]:
    """Every player ID the `player` table holds."""
    return set(session.scalars(select(Player.nba_player_id)))


def rosters(session: Session, dates: Collection[date]) -> dict[tuple[int, date], set[int]]:
    if not dates:
        return {}
    spans = session.execute(
        select(
            RosterMembership.player_id,
            RosterMembership.team_id,
            RosterMembership.start_date,
            RosterMembership.end_date,
        ).where(
            RosterMembership.start_date <= max(dates),
            or_(RosterMembership.end_date.is_(None), RosterMembership.end_date >= min(dates)),
        )
    )
    found: dict[tuple[int, date], set[int]] = {}
    for player_id, team_id, start, end in spans:
        for day in dates:
            if start <= day and (end is None or day <= end):
                found.setdefault((team_id, day), set()).add(player_id)
    return found
