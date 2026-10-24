"""Hoops Rumors' two seasonal contract articles: every player without a fully guaranteed salary,
and the dates before January 7 on which some of those salaries guarantee (spec §4.6, ADR-0004).

The terms (checked 2026-10-23). Spotrac's Terms of Service forbid scrapers and RealGM's require
"click-by-click selections directly by a human being", and both answer a non-browser client with
403, so neither is used. Hoops Rumors' robots.txt allows the articles (`User-agent: *`,
`Crawl-delay: 1`; it disallows `/search` and `/*?s`), it publishes no terms of use, only a privacy
policy, and plain httpx is served. Each season it maintains, until January 10:

- "<season> Non-Guaranteed Contracts By Team": an `<h3>` per team, by its full name, and under it
  a `<ul>` of player lines, `Name` or `Name ($15,992,957)`, each optionally followed by a nested
  `Partially guaranteed for $2,000,000.`; a team with none has the line `None`. The players are
  "on minimum-salary contracts" unless an amount is given. An `Updated 10-22-26 (7:08 am CT)`
  stamp follows the `<hr>` that opens the list. Two-way contracts are not on it.
- "Early NBA Salary Guarantee Dates For <season>": after an `<hr>`, a `<p><strong>` heading per
  date, `June 28`, or one of two named ones (`First day of NBA regular season`, `First day of
  team's regular season`), and under it a `<ul>` of lines, `Name (Team): Non-guaranteed salary
  ($X) becomes fully guaranteed.` and the like, the team by its nickname. A line ends `(✅)` once
  the date has passed with the player kept, `(❌)` when he was waived before it.

The articles move each season and the month in their path varies, and robots.txt rules out a
search, so their URLs are committed in `ARTICLES`, added by hand when they appear (May to
July). `fetch` keeps the day's copies as
`raw/hoopsrumors/{non_guaranteed,guarantee_dates}/<date>.html`, so the `contracts` overlay re-run
on the same day reads the same pages.
"""

import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Final, Literal, NamedTuple

import httpx

from closinglineup.sources.cache import RawCache
from closinglineup.teams import TEAMS, UnknownTeam, by_name

# `Final`, so the overlay can pass it where a `ContractSource` literal is wanted.
SOURCE: Final = "hoopsrumors"
NON_GUARANTEED = "non_guaranteed"
GUARANTEE_DATES = "guarantee_dates"
SUFFIX = ".html"
# robots.txt's `Crawl-delay`.
CRAWL_DELAY = timedelta(seconds=1)
# A list with fewer team headings than this is not the layout `parse_non_guaranteed` reads.
MIN_TEAM_HEADINGS = 30


class Articles(NamedTuple):
    non_guaranteed: str
    guarantee_dates: str


# By season label; add next season's when Hoops Rumors publishes them.
ARTICLES: dict[str, Articles] = {
    "2026-27": Articles(
        "https://www.hoopsrumors.com/2026/07/2026-27-non-guaranteed-contracts-by-team.html",
        "https://www.hoopsrumors.com/2026/05/early-nba-salary-guarantee-dates-for-2026-27.html",
    ),
}

# Each article's title, which a page must carry in its `<h1 class="entry-title">` to be cached.
_TITLES = {
    NON_GUARANTEED: re.compile(r'<h1 class="entry-title">[^<]*Non-Guaranteed Contracts By Team'),
    GUARANTEE_DATES: re.compile(r'<h1 class="entry-title">[^<]*Salary Guarantee Dates'),
}
# Hoops Rumors' spellings of a team that differ from `teams.py`'s.
_TEAM_NAMES = {"Los Angeles Clippers": "LA Clippers"}
_NICKNAMES = {"Sixers": "76ers"}
# The months a date heading falls in the season's first year; the rest are in its second.
_FIRST_YEAR_MONTHS = range(6, 13)
_LEAGUE_OPENER = "First day of NBA regular season"
_TEAM_OPENER = "First day of team's regular season"

_UPDATED = re.compile(r"Updated (\d{1,2})-(\d{1,2})-(\d{2})\b")
# A name has no digits, parentheses, dollar signs or colons, so a note is not read as a player.
_LISTED = re.compile(r"(?P<player>[^\d()$:]+?)(?: \(\$(?P<salary>[\d,]+)\))?")
_PARTIAL = re.compile(r"Partially guaranteed for \$(?P<partial>[\d,]+)\.?")
_GUARANTEE = re.compile(
    r"(?P<player>.+?) \((?P<team>[^()]+)\): "
    r"(?:Non-guaranteed salary \(\$(?P<salary>[\d,]+)\)"
    r"|Partial guarantee \(\$(?P<partial>[\d,]+)\))"
    r" (?:becomes|increases to) "
    r"(?:(?P<fully>fully guaranteed)|full guarantee \(\$(?P<full>[\d,]+)\)"
    r"|partially guaranteed \(\$(?P<to_partial>[\d,]+)\)|\$(?P<bare>[\d,]+))"
    r"\.(?: \((?P<mark>[✅❌])\))?"
)

log = logging.getLogger(__name__)

# A named heading's date, which the parser leaves to the overlay: the season's first counted game,
# or the line's team's.
type Opener = Literal["league_opener", "team_opener"]


class HoopsRumorsError(ValueError):
    """A season with no articles, or a page that does not read as its article."""


@dataclass(frozen=True, slots=True)
class NonGuaranteedLine:
    """A player on the non-guaranteed list. `salary` is None for a minimum-salary contract, and
    `partial` where the list gives no partial guarantee."""

    team_id: int
    # As the list spells it, e.g. "Duncan Robinson".
    player: str
    salary: int | None
    partial: int | None


@dataclass(frozen=True, slots=True)
class NonGuaranteedList:
    # The list's "Updated" stamp; None where it has none.
    updated: date | None
    lines: list[NonGuaranteedLine]


@dataclass(frozen=True, slots=True)
class GuaranteeLine:
    """A player's guarantee rising on `day` from `before` to `after` dollars. `salary` is the line's
    full-salary figure, where it gives one; `settled` that the date has passed, the player kept or
    waived."""

    day: date | Opener
    team_id: int
    player: str
    before: int
    salary: int | None
    after: int
    settled: bool


def articles(season: str) -> Articles:
    try:
        return ARTICLES[season]
    except KeyError:
        raise HoopsRumorsError(
            f"no Hoops Rumors articles for {season}; add them to ARTICLES"
        ) from None


def fetch(client: httpx.Client, cache: RawCache, day: date, season: str) -> tuple[Path, Path]:
    """The non-guaranteed list's and the guarantee dates' pages cached under `day`, the date they
    are fetched for; a cached copy is not re-fetched.

    Only a 200 whose body carries the article's title and parses is cached; anything else raises,
    and neither page is cached. The requests are `CRAWL_DELAY` apart.
    """
    key = day.isoformat()
    urls = articles(season)
    fetched: list[tuple[str, str, bytes]] = []
    last: datetime | None = None
    pages = ((NON_GUARANTEED, urls.non_guaranteed), (GUARANTEE_DATES, urls.guarantee_dates))
    for endpoint, url in pages:
        if cache.exists(SOURCE, endpoint, key, suffix=SUFFIX):
            continue
        if last is not None:
            cache.clock.sleep_until(last + CRAWL_DELAY)
        last = cache.clock.now()
        response = client.get(url, follow_redirects=True)
        response.raise_for_status()
        if not _TITLES[endpoint].search(response.text):
            raise HoopsRumorsError(f"response from {url} is not the article")
        if endpoint == NON_GUARANTEED:
            _non_guaranteed(response.text, url)
        else:
            _guarantee_dates(response.text, season, url)
        fetched.append((endpoint, url, response.content))
    for endpoint, url, content in fetched:
        cache.put(SOURCE, endpoint, key, content, url=url, status=200, suffix=SUFFIX)
        log.info("fetched", extra={"source": SOURCE, "endpoint": endpoint, "key": key})
    return (
        cache.path(SOURCE, NON_GUARANTEED, key, suffix=SUFFIX),
        cache.path(SOURCE, GUARANTEE_DATES, key, suffix=SUFFIX),
    )


def parse_non_guaranteed(path: Path) -> NonGuaranteedList:
    """Every player line of the non-guaranteed list cached at `path`, in the list's order.

    A team heading that names no NBA team, fewer than `MIN_TEAM_HEADINGS` of them, or a line that
    does not read as a player's raises `HoopsRumorsError`.
    """
    return _non_guaranteed(path.read_text(encoding="utf-8"), path.name)


def parse_guarantee_dates(path: Path, season: str) -> list[GuaranteeLine]:
    """Every line of the guarantee dates cached at `path`, for `season`, in the article's order.

    A heading that is neither a date nor a named one, or a line that does not match the
    article's pattern, raises `HoopsRumorsError`.
    """
    return _guarantee_dates(path.read_text(encoding="utf-8"), season, path.name)


def _non_guaranteed(html: str, name: str) -> NonGuaranteedList:
    updated: date | None = None
    team_id: int | None = None
    headings = 0
    lines: list[NonGuaranteedLine] = []
    for block in _blocks(html):
        text = block.text
        if block.tag == "p" and (stamp := _UPDATED.search(text)) is not None:
            month, day, year = (int(part) for part in stamp.groups())
            updated = date(2000 + year, month, day)
        elif block.tag == "h3":
            team_id = _team_by_name(text, name)
            headings += 1
        elif block.tag != "li" or team_id is None or text == "None":
            continue
        elif block.nested:
            note = _PARTIAL.fullmatch(text)
            if note is None or not lines:
                raise HoopsRumorsError(f"{name}: unreadable note {text!r}")
            lines[-1] = replace(lines[-1], partial=_dollars(note["partial"]))
        else:
            listed = _LISTED.fullmatch(text)
            if listed is None:
                raise HoopsRumorsError(f"{name}: unreadable player line {text!r}")
            salary = listed["salary"]
            lines.append(
                NonGuaranteedLine(
                    team_id, listed["player"], _dollars(salary) if salary else None, None
                )
            )
    if headings < MIN_TEAM_HEADINGS:
        raise HoopsRumorsError(
            f"{name}: {headings} team headings, fewer than {MIN_TEAM_HEADINGS}; "
            "has the layout changed?"
        )
    return NonGuaranteedList(updated, lines)


def _guarantee_dates(html: str, season: str, name: str) -> list[GuaranteeLine]:
    start = int(season[:4])
    listing = False
    day: date | Opener | None = None
    lines: list[GuaranteeLine] = []
    for block in _blocks(html):
        if block.tag == "hr":
            listing = True
        elif not listing or block.nested:
            continue
        elif block.tag == "p":
            day = _heading(block.text, start, name)
        elif block.tag == "li":
            if day is None:
                raise HoopsRumorsError(f"{name}: a line before any date heading")
            lines.append(_guarantee_line(block.text, day, name))
    return lines


def _heading(text: str, start: int, name: str) -> date | Opener:
    # The article spells the named heading with a curly apostrophe.
    plain = text.replace("\u2019", "'")
    if plain == _LEAGUE_OPENER:
        return "league_opener"
    if plain == _TEAM_OPENER:
        return "team_opener"
    try:
        # A leap year, so February 29 parses; the season's year is applied after.
        parsed = datetime.strptime(f"{plain} 2000", "%B %d %Y")
    except ValueError:
        raise HoopsRumorsError(f"{name}: {text!r} is not a date heading") from None
    year = start if parsed.month in _FIRST_YEAR_MONTHS else start + 1
    return date(year, parsed.month, parsed.day)


def _guarantee_line(text: str, day: date | Opener, name: str) -> GuaranteeLine:
    found = _GUARANTEE.fullmatch(text)
    if found is None:
        raise HoopsRumorsError(f"{name}: unreadable guarantee line {text!r}")
    non_guaranteed = found["salary"]
    full = found["full"]
    salary = _dollars(non_guaranteed or full) if non_guaranteed or full else None
    if found["fully"]:
        if salary is None:
            raise HoopsRumorsError(f"{name}: {text!r} gives no full salary")
        after = salary
    else:
        after = _dollars(full or found["to_partial"] or found["bare"])
    partial = found["partial"]
    return GuaranteeLine(
        day,
        _team_by_nickname(found["team"], name),
        found["player"],
        _dollars(partial) if partial else 0,
        salary,
        after,
        found["mark"] is not None,
    )


def _team_by_name(text: str, name: str) -> int:
    try:
        return by_name(_TEAM_NAMES.get(text, text)).nba_team_id
    except UnknownTeam:
        raise HoopsRumorsError(f"{name}: heading {text!r} names no NBA team") from None


def _team_by_nickname(nickname: str, name: str) -> int:
    nickname = _NICKNAMES.get(nickname, nickname)
    found = [team for team in TEAMS if team.name.endswith(f" {nickname}")]
    if len(found) != 1:
        raise HoopsRumorsError(f"{name}: {nickname!r} names no one NBA team")
    return found[0].nba_team_id


def _dollars(amount: str) -> int:
    return int(amount.replace(",", ""))


@dataclass(slots=True)
class _Block:
    """An `<h3>`, `<p>`, `<li>` or `<hr>` of the article body, with its own text: a block nested in
    it has its own. `nested` is a list item inside another, a note under a player's line."""

    tag: str
    nested: bool
    parts: list[str]

    @property
    def text(self) -> str:
        # The variation selector an emoji may carry is dropped.
        return " ".join("".join(self.parts).replace("\ufe0f", "").split())


class _Body(HTMLParser):
    """The blocks of the page's `entry-content` div, the article body, in document order."""

    BLOCKS = frozenset({"h3", "p", "li"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[_Block] = []
        self._open: list[_Block] = []
        # How many divs deep inside the body; 0 outside it.
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._depth:
            classes = (dict(attrs).get("class") or "").split()
            if tag == "div" and "entry-content" in classes:
                self._depth = 1
        elif tag == "div":
            self._depth += 1
        elif tag == "hr":
            self.blocks.append(_Block(tag, False, []))
        elif tag in self.BLOCKS:
            nested = tag == "li" and any(block.tag == "li" for block in self._open)
            block = _Block(tag, nested, [])
            self.blocks.append(block)
            self._open.append(block)
        elif tag == "br" and self._open:
            self._open[-1].parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if not self._depth:
            return
        if tag == "div":
            self._depth -= 1
        elif tag in self.BLOCKS:
            # Close the innermost open block of the tag, and any left open inside it; a stray end
            # tag closes nothing.
            for i in range(len(self._open) - 1, -1, -1):
                if self._open[i].tag == tag:
                    del self._open[i:]
                    break

    def handle_data(self, data: str) -> None:
        if self._depth and self._open:
            self._open[-1].parts.append(data)


def _blocks(html: str) -> list[_Block]:
    body = _Body()
    body.feed(html)
    body.close()
    return body.blocks
