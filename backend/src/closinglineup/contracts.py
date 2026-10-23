"""The `contracts` overlay: Hoops Rumors' non-guaranteed list into `contract` (spec §4.6, ADR-0004).

The `weekly` job runs it after the shot charts (spec §3.3); `closinglineup overlays contracts`
runs it once. Each player on the list becomes the run date's `source='hoopsrumors'` row for his
team: the salary the list gives, else the full salary one of his guarantee-date lines gives, else
NULL, a minimum-salary contract; the partial guarantee the list gives, else 0; and the date his
guarantee next rises, with the amount it rises to. That is his first unsettled guarantee-date
line on or after the run date, else January 7 of the season's second year and his full salary,
the league-wide default. A player absent from the list is taken as fully guaranteed.

Names resolve by the name index and the alias table against the team's roster on the run date.
One that does not is logged and has no row, and the run's detail lists it with the `aliases.yml`
line to add for it. A guarantee-date line whose named heading's date is not yet known, because the
season's games are not loaded, is passed over with a warning.

The pages are today's, so `day` must be today, in `TZ`, unless both are already cached. Each run
makes the run date's rows exactly the list, so a re-run on the same day reads the day's cached
pages and writes nothing; earlier weeks' rows are kept. With `HOOPSRUMORS_ENABLED=false` (the
default) the run is a no-op: no request, no rows.
"""

import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import delete, func, select, tuple_, union_all
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from closinglineup.db.models import Base, Game
from closinglineup.names import NameIndex
from closinglineup.resolve import Rosters, name_index, rosters
from closinglineup.season import Phase, season_label
from closinglineup.sources.hoopsrumors import (
    GUARANTEE_DATES,
    NON_GUARANTEED,
    SOURCE,
    SUFFIX,
    GuaranteeLine,
    NonGuaranteedLine,
    fetch,
    parse_guarantee_dates,
    parse_non_guaranteed,
)
from closinglineup.teams import by_nba_team_id

if TYPE_CHECKING:
    from closinglineup.scheduler import Jobs

TABLE = "contract"
NATURAL_KEY = ("player_id", "team_id", "season", "as_of_date", "source")
VALUES = ("salary", "minimum_salary", "guaranteed", "guarantee_date", "guaranteed_after")
# The league-wide guarantee date, in the season's second year: a player still under contract
# through it receives his full salary.
DEFAULT_GUARANTEE = (1, 7)

type ContractSource = Literal["hoopsrumors"]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ContractRow:
    """A `contract` row's column values. `salary` and `guaranteed_after` are None for a
    minimum-salary contract whose amount no line gives."""

    player_id: int
    team_id: int
    season: str
    salary: int | None
    minimum_salary: bool
    guaranteed: int
    guarantee_date: date
    guaranteed_after: int | None
    as_of_date: date
    source: ContractSource


@dataclass(frozen=True, slots=True)
class Openers:
    """The dates the guarantee dates' named headings stand for: the season's first regular-season
    game, and each team's; None and absent where the games are not loaded."""

    league: date | None
    teams: Mapping[int, date]


@dataclass(frozen=True, slots=True)
class Built:
    rows: list[ContractRow]
    # Each list line whose player could not be resolved, as "Name (TRI)".
    unresolved: list[str]
    # The `aliases.yml` line to add for each, "Name: <player ID>".
    aliases_to_add: list[str]
    # Each list line naming a player already listed, which has no row, as "Name (TRI)".
    duplicates: list[str]
    # Each guarantee-date line for a player and team with no row, as "Name (TRI)".
    ignored_lines: list[str]


def build_rows(
    listed: list[NonGuaranteedLine],
    dates: list[GuaranteeLine],
    day: date,
    season: str,
    index: NameIndex,
    on_rosters: Rosters,
    openers: Openers,
) -> Built:
    """The list's lines as `day`'s `contract` rows for `season`, each with its guarantee dates."""

    def resolve(player: str, team_id: int) -> int | None:
        return index.resolve(player, on_rosters.get((team_id, day), ()))

    dated = [(resolve(line.player, line.team_id), line) for line in dates]
    rows: list[ContractRow] = []
    unresolved: list[str] = []
    aliases: list[str] = []
    duplicates: list[str] = []
    seen: set[int] = set()
    for line in listed:
        player_id = resolve(line.player, line.team_id)
        if player_id is None:
            log.warning("Hoops Rumors player not resolved", extra={"line": line.player})
            unresolved.append(_labelled(line.player, line.team_id))
            aliases.append(f"{line.player}: <player ID>")
            continue
        if player_id in seen:
            log.warning("Hoops Rumors lists a player twice", extra={"line": line.player})
            duplicates.append(_labelled(line.player, line.team_id))
            continue
        seen.add(player_id)
        his = [d for pid, d in dated if pid == player_id and d.team_id == line.team_id]
        rows.append(_row(line, player_id, his, day, season, openers))
    keys = {(row.player_id, row.team_id) for row in rows}
    ignored = [
        _labelled(line.player, line.team_id)
        for player_id, line in dated
        if (player_id, line.team_id) not in keys
    ]
    return Built(rows, unresolved, aliases, duplicates, ignored)


def _row(
    line: NonGuaranteedLine,
    player_id: int,
    dates: list[GuaranteeLine],
    day: date,
    season: str,
    openers: Openers,
) -> ContractRow:
    salary = line.salary
    if salary is None:
        salary = next((d.salary for d in dates if d.salary is not None), None)
    upcoming = [
        (on, d)
        for d in dates
        if not d.settled and (on := _date_of(d, openers)) is not None and on >= day
    ]
    if upcoming:
        guarantee_date, next_line = min(upcoming, key=lambda pair: pair[0])
        after: int | None = next_line.after
    else:
        guarantee_date, after = date(int(season[:4]) + 1, *DEFAULT_GUARANTEE), salary
    return ContractRow(
        player_id,
        line.team_id,
        season,
        salary,
        line.salary is None,
        line.partial or 0,
        guarantee_date,
        after,
        day,
        SOURCE,
    )


def _date_of(line: GuaranteeLine, openers: Openers) -> date | None:
    if isinstance(line.day, date):
        return line.day
    found = openers.league if line.day == "league_opener" else openers.teams.get(line.team_id)
    if found is None:
        log.warning(
            "Hoops Rumors guarantee date not known", extra={"line": line.player, "day": line.day}
        )
    return found


def _labelled(player: str, team_id: int) -> str:
    return f"{player} ({by_nba_team_id(team_id).tricode})"


def openers(session: Session, season: str) -> Openers:
    """Each team's first regular-season game date of `season`, and the earliest of them."""
    regular = (Game.season == season, Game.phase == Phase.REGULAR.value)
    sides = union_all(
        select(Game.home_team_id.label("team_id"), Game.game_date).where(*regular),
        select(Game.away_team_id.label("team_id"), Game.game_date).where(*regular),
    ).subquery()
    first = select(sides.c.team_id, func.min(sides.c.game_date)).group_by(sides.c.team_id)
    teams: dict[int, date] = {team_id: day for team_id, day in session.execute(first)}
    return Openers(min(teams.values(), default=None), teams)


def replace_contracts(session: Session, day: date, rows: list[ContractRow]) -> tuple[int, int]:
    """Make `day`'s `hoopsrumors` rows exactly `rows`: insert each the table lacks, update each
    whose values changed, and delete each player-team row `rows` lack; an unchanged row is not
    written, `updated_at` included. Returns (inserted or updated, deleted).

    `rows` must be for `day`, and must not repeat a player and team. As
    `impact.replace_snapshot`, but on `contract`'s natural key, which its `id` does not replace.
    """
    table = Base.metadata.tables[TABLE]
    stale = delete(table).where(table.c.as_of_date == day, table.c.source == SOURCE)
    if rows:
        stale = stale.where(
            tuple_(table.c.player_id, table.c.team_id).not_in(
                [(row.player_id, row.team_id) for row in rows]
            )
        )
    removed = len(session.execute(stale.returning(table.c.id)).all())
    if not rows:
        return 0, removed
    stmt = insert(table).values([asdict(row) for row in rows])
    stmt = stmt.on_conflict_do_update(
        index_elements=NATURAL_KEY,
        set_={**{name: stmt.excluded[name] for name in VALUES}, "updated_at": func.now()},
        where=tuple_(*(table.c[name] for name in VALUES)).is_distinct_from(
            tuple_(*(stmt.excluded[name] for name in VALUES))
        ),
    )
    # Counted from RETURNING, as the loader does: rows the WHERE skips are not returned.
    changed = len(session.execute(stmt.returning(table.c.id)).all())
    return changed, removed


def run(jobs: "Jobs", day: date) -> dict[str, Any]:
    """Fetch today's pages for `day`, or read the copies cached for it, and store the list's rows;
    nothing when `HOOPSRUMORS_ENABLED` is off."""
    if not jobs.settings.hoopsrumors_enabled:
        return {"enabled": False, "rows": 0}
    season = season_label(day)
    today = jobs.clock.now().astimezone(jobs.settings.tz).date()
    key = day.isoformat()
    cached = all(
        jobs.cache.exists(SOURCE, endpoint, key, suffix=SUFFIX)
        for endpoint in (NON_GUARANTEED, GUARANTEE_DATES)
    )
    if day != today and not cached:
        raise ValueError(f"the pages are today's, {today.isoformat()}; {key} is not cached")
    with jobs.http() as client:
        listed_path, dates_path = fetch(client, jobs.cache, day, season)
    listed = parse_non_guaranteed(listed_path)
    dates = parse_guarantee_dates(dates_path, season)
    with jobs.db.begin() as session:
        built = build_rows(
            listed.lines,
            dates,
            day,
            season,
            name_index(session),
            rosters(session, [day]),
            openers(session, season),
        )
        changed, removed = replace_contracts(session, day, built.rows)
    return {
        "enabled": True,
        "files": [f"{NON_GUARANTEED}/{listed_path.name}", f"{GUARANTEE_DATES}/{dates_path.name}"],
        "updated": listed.updated.isoformat() if listed.updated else None,
        "season": season,
        "rows": len(built.rows),
        "changed": changed,
        "removed": removed,
        "unresolved": built.unresolved,
        "aliases_to_add": built.aliases_to_add,
        "duplicates": built.duplicates,
        "ignored_lines": built.ignored_lines,
    }
