"""The NBA's official injury report: finding the latest PDF and parsing it (spec §4.3).

The report is published every 15 minutes, around the clock, at

    https://ak-static.cms.nba.com/referee/injury/Injury-Report_<YYYY-MM-DD>_<HH>_<MM><AM|PM>.pdf

stamped with its Eastern time, e.g. `Injury-Report_2026-10-22_05_00PM.pdf`. Probed 2026-10-22 and
23 from the home network: every quarter hour of 2026-10-22 answered 200, and a report not yet
published answers 403, not 404. Until partway through 2025-26 the stamp had no minutes
(`Injury-Report_2025-10-22_05PM.pdf`, hourly); that form now answers 403 and is not tried. No
report is published on a day with no games either side of it: none on 2026-02-16 or 17, in the
All-Star break, while 2025-12-24 had them for Christmas Day's games.

`discover` tries the newest quarter hours of a date first, through `NbaSession`, and keeps the
first report it finds in the raw cache as `raw/injury/report/<report time>.pdf`. `parse` reads one
with pdfplumber; this is the only module that imports it (spec §3.1).

The report is one table, drawn with a ruled line under every row, whose game date, game time,
matchup and team cells are left blank under the row that names them. A reason too long for its
cell wraps onto a second line, and at a page break the rest of it starts the next page.
"""

import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pdfplumber
from pdfplumber.page import Page

from closinglineup.db.models import INJURY_STATUSES
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_http import NbaHttpError, NbaSession
from closinglineup.teams import TEAMS

SOURCE = "injury"
ENDPOINT = "report"
PDF_SUFFIX = ".pdf"
URL = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_{stamp}.pdf"
URL_STAMP = "%Y-%m-%d_%I_%M%p"
# The cache key: the report's Eastern time, sortable.
KEY_FORMAT = "%Y-%m-%dT%H%M"
# The report's times are Eastern whatever `TZ` is.
EASTERN = ZoneInfo("America/New_York")
PUBLISHED_EVERY = timedelta(minutes=15)
# How many quarter hours `discover` tries, newest first, before giving up: two hours' worth.
DISCOVERY_TRIES = 8
# What ak-static.cms.nba.com answers for a report that is not (yet) there.
NOT_PUBLISHED = frozenset({403, 404})

NOT_YET_SUBMITTED = "NOT YET SUBMITTED"
COLUMNS = 7
_TITLE = re.compile(r"Injury Report: (\d\d/\d\d/\d\d \d\d:\d\d [AP]M)")
_FOOTER = re.compile(r"Page\d*(of\d+)?")
_MATCHUP = re.compile(r"([A-Z]{3})@([A-Z]{3})")
# Spaces between words are not characters in the PDF, only gaps; a gap between words is about
# 2.3 points, so pdfplumber's default tolerance of 3 runs every word of a cell together.
_X_TOLERANCE = 1.5
_TEAMS_BY_NAME = {team.name: team.tricode for team in TEAMS}

log = logging.getLogger(__name__)


class InjuryReportNotFoundError(LookupError):
    """No report was published in the quarter hours `discover` tried."""


class InjuryReportParseError(ValueError):
    """A PDF that does not read as an injury report: the layout or a value is not one known."""


@dataclass(frozen=True, slots=True)
class InjuryStatusRow:
    """One player's line in a report, with the game, team and name as the report gives them."""

    game_date: date
    away: str
    home: str
    team: str
    # "Last, First", e.g. "McCullar Jr., Kevin".
    player: str
    status: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class InjuryReport:
    report_datetime: datetime
    rows: list[InjuryStatusRow]
    # (game date, team tricode) of each team whose report was not yet submitted.
    not_submitted: list[tuple[date, str]]


def report_url(at: datetime) -> str:
    return URL.format(stamp=at.astimezone(EASTERN).strftime(URL_STAMP))


def report_key(at: datetime) -> str:
    return at.astimezone(EASTERN).strftime(KEY_FORMAT)


def candidates(day: date, now: datetime) -> list[datetime]:
    """The quarter hours of `day` a report may be stamped with, no later than `now`, newest first,
    at most `DISCOVERY_TRIES` of them: the last of the day for a past date, none for a future one.
    """
    local = now.astimezone(EASTERN)
    latest = datetime.combine(day, time(23, 45), EASTERN)
    if local.date() == day:
        latest = local.replace(minute=local.minute - local.minute % 15, second=0, microsecond=0)
    elif local.date() < day:
        return []
    found: list[datetime] = []
    at = latest
    while at.date() == day and len(found) < DISCOVERY_TRIES:
        found.append(at)
        at -= PUBLISHED_EVERY
    return found


def discover(day: date, session: NbaSession, cache: RawCache, now: datetime) -> Path:
    """The cached PDF of the latest report stamped on `day` by `now`, fetching it on a miss.

    Each candidate quarter hour, newest first, is read from the cache if it is there and fetched
    otherwise; a 403 or 404 moves on to the one before. Only a 200 whose body is a PDF is cached.
    """
    for at in candidates(day, now):
        key = report_key(at)
        path = cache.path(SOURCE, ENDPOINT, key, suffix=PDF_SUFFIX)
        if path.is_file():
            return path
        url = report_url(at)
        response = session.get(url)
        if response.status_code in NOT_PUBLISHED:
            continue
        if response.status_code != 200:
            raise NbaHttpError(f"HTTP {response.status_code} from {url}")
        if not response.content.startswith(b"%PDF"):
            raise NbaHttpError(f"response from {url} is not a PDF")
        cache.put(
            SOURCE,
            ENDPOINT,
            key,
            response.content,
            url=url,
            status=response.status_code,
            suffix=PDF_SUFFIX,
        )
        log.info("fetched", extra={"source": SOURCE, "endpoint": ENDPOINT, "key": key})
        return path
    raise InjuryReportNotFoundError(f"no injury report published on {day} by {now.isoformat()}")


def parse(pdf_path: Path) -> InjuryReport:
    """Every player line of the report at `pdf_path`.

    A team whose report is "NOT YET SUBMITTED" has no lines; it is logged as a warning and listed
    in `not_submitted`. A team, matchup or status the report should not hold raises
    `InjuryReportParseError`.
    """
    with pdfplumber.open(pdf_path) as pdf:
        pages = list(pdf.pages)
        if not pages:
            raise InjuryReportParseError(f"{pdf_path.name} has no pages")
        report_datetime = _report_datetime(pages[0])
        edges = _column_edges(pages[0])
        cells = [_page_rows(page, edges) for page in pages]
    return _read_rows(report_datetime, cells, pdf_path.name)


def _report_datetime(page: Page) -> datetime:
    title = _TITLE.search(page.extract_text() or "")
    if title is None:
        raise InjuryReportParseError("no 'Injury Report: <date> <time>' title on the first page")
    return datetime.strptime(title[1], "%m/%d/%y %I:%M %p").replace(tzinfo=EASTERN)


def _column_edges(page: Page) -> list[float]:
    """The x of each column's left edge and the last one's right, from the header row's boxes,
    which every page shares."""
    reason = next(
        (w for w in page.extract_words(x_tolerance=_X_TOLERANCE) if w["text"] == "Reason"), None
    )
    if reason is None:
        raise InjuryReportParseError("no header row on the first page")
    middle = (reason["top"] + reason["bottom"]) / 2
    boxes = [box for box in page.rects if box["top"] < middle < box["bottom"]]
    edges = sorted({round(box["x0"], 1) for box in boxes} | {round(box["x1"], 1) for box in boxes})
    if len(edges) != COLUMNS + 1:
        raise InjuryReportParseError(f"the header row has {len(edges) - 1} columns, not {COLUMNS}")
    return edges


def _page_rows(page: Page, edges: list[float]) -> list[list[str]]:
    """The page's table rows, each a cleaned cell per column, rows with no text left out.

    The table runs from under the title, or the header row on the first page, to the footer, and
    each row ends at a ruled line.
    """
    words = page.extract_words()
    heads = [word["bottom"] for word in words if word["text"] in {"Report:", "Reason"}]
    if not heads:
        raise InjuryReportParseError(f"page {page.page_number} has no 'Injury Report:' title")
    top = max(heads)
    footers = [word["top"] for word in words if _FOOTER.fullmatch(word["text"])]
    bottom = min(footers) if footers else page.height
    rules = sorted({round(line["top"], 1) for line in page.lines if top < line["top"] < bottom})
    settings: dict[str, Any] = {
        "vertical_strategy": "explicit",
        "explicit_vertical_lines": edges,
        "horizontal_strategy": "explicit",
        "explicit_horizontal_lines": [top + 0.5, *rules, bottom - 0.5],
        "text_x_tolerance": _X_TOLERANCE,
    }
    rows: list[list[str]] = []
    for row in page.extract_table(settings) or []:
        cells = [" ".join((cell or "").split()) for cell in row]
        if any(cells):
            rows.append(cells)
    return rows


def _read_rows(report_datetime: datetime, pages: list[list[list[str]]], name: str) -> InjuryReport:
    """The report's lines from its pages' rows, carrying each blank game date, matchup and team
    down from the row above, and joining each reason split across rows back together.

    A row with only a reason continues one cut by a page break: the previous page's last line if
    it starts a page, else the next line, whose reason it begins.
    """
    rows: list[InjuryStatusRow] = []
    not_submitted: list[tuple[date, str]] = []
    game_date: date | None = None
    matchup: tuple[str, str] | None = None
    team: str | None = None
    pending: list[str] = []
    for page in pages:
        for index, (day, _, teams, team_name, player, status, reason) in enumerate(page):
            if day:
                game_date = datetime.strptime(day, "%m/%d/%Y").date()
            if teams:
                found = _MATCHUP.fullmatch(teams)
                if found is None:
                    raise InjuryReportParseError(f"{name}: matchup {teams!r} is not AAA@HHH")
                matchup = (found[1], found[2])
            if team_name:
                team = _TEAMS_BY_NAME.get(team_name)
                if team is None:
                    raise InjuryReportParseError(f"{name}: unknown team {team_name!r}")
            if not player and not status:
                if reason == NOT_YET_SUBMITTED:
                    day_of, _, team_of = _context(game_date, matchup, team, name)
                    log.warning(
                        "injury report not yet submitted",
                        extra={"report": name, "game_date": day_of.isoformat(), "team": team_of},
                    )
                    not_submitted.append((day_of, team_of))
                elif reason and index == 0 and rows and not pending:
                    rows[-1] = _extend_reason(rows[-1], reason)
                elif reason:
                    pending.append(reason)
                continue
            if status not in INJURY_STATUSES:
                raise InjuryReportParseError(f"{name}: {player!r} has unknown status {status!r}")
            day_of, (away, home), team_of = _context(game_date, matchup, team, name)
            if team_of not in (away, home):
                raise InjuryReportParseError(f"{name}: {team_of} is not in {away}@{home}")
            full_reason = " ".join([*pending, reason]).strip() or None
            pending = []
            rows.append(InjuryStatusRow(day_of, away, home, team_of, player, status, full_reason))
    if pending:
        raise InjuryReportParseError(f"{name}: reason text {' '.join(pending)!r} has no line")
    return InjuryReport(report_datetime, rows, not_submitted)


def _context(
    game_date: date | None, matchup: tuple[str, str] | None, team: str | None, name: str
) -> tuple[date, tuple[str, str], str]:
    """The game date, matchup and team a line falls under."""
    if game_date is None or matchup is None or team is None:
        raise InjuryReportParseError(f"{name}: a line comes before its game date, matchup or team")
    return game_date, matchup, team


def _extend_reason(row: InjuryStatusRow, more: str) -> InjuryStatusRow:
    return replace(row, reason=f"{row.reason} {more}" if row.reason else more)
