import re
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from closinglineup.clock import FakeClock
from closinglineup.sources import injury_pdf
from closinglineup.sources.cache import RawCache
from closinglineup.sources.injury_pdf import (
    EASTERN,
    InjuryReportNotFoundError,
    InjuryStatusRow,
    candidates,
    discover,
    parse,
    report_url,
)
from closinglineup.sources.nba_http import NbaHttpError, NbaSession, RateLimiter
from tests.fakes import Connect, ScriptedGet, fake_response

FIXTURES = Path(__file__).parent / "fixtures" / "injury_pdf"
SRC = Path(__file__).parent.parent / "src"
WEEKDAY = FIXTURES / "2026-10-22T1700.pdf"
WEEKEND = FIXTURES / "2026-03-01T1715.pdf"
MORNING = FIXTURES / "2026-10-22T0900.pdf"


def _eastern(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=EASTERN)


# Hand-counted from each PDF's words (fixtures README): lines, their statuses, and how many teams
# had not yet submitted.
@pytest.mark.parametrize(
    ("path", "reported", "statuses", "not_submitted"),
    [
        (
            WEEKDAY,
            _eastern(2026, 10, 22, 17, 0),
            {"Out": 36, "Questionable": 16, "Doubtful": 2, "Probable": 6, "Available": 3},
            8,
        ),
        (
            WEEKEND,
            _eastern(2026, 3, 1, 17, 15),
            {"Out": 106, "Questionable": 10, "Doubtful": 2, "Probable": 1, "Available": 15},
            6,
        ),
        (MORNING, _eastern(2026, 10, 22, 9, 0), {"Out": 9, "Questionable": 3}, 24),
    ],
)
def test_each_fixture_parses_to_its_hand_counted_lines(
    path: Path, reported: datetime, statuses: dict[str, int], not_submitted: int
) -> None:
    report = parse(path)

    assert report.report_datetime == reported
    assert len(report.rows) == sum(statuses.values())
    assert Counter(row.status for row in report.rows) == statuses
    assert len(report.not_submitted) == not_submitted


def test_a_team_not_yet_submitted_has_no_lines_and_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    report = parse(WEEKDAY)

    # HOU at SAS on 2026-10-23: HOU had submitted, SAS not.
    assert (date(2026, 10, 23), "SAS") in report.not_submitted
    assert (date(2026, 10, 23), "HOU") not in report.not_submitted
    lines = {(row.game_date, row.team) for row in report.rows}
    assert (date(2026, 10, 23), "HOU") in lines
    assert not lines & set(report.not_submitted)
    warned = [r for r in caplog.records if r.getMessage() == "injury report not yet submitted"]
    assert len(warned) == 8


def test_game_date_matchup_and_team_carry_down_to_the_lines_below() -> None:
    rows = parse(WEEKDAY).rows

    assert rows[0] == InjuryStatusRow(
        date(2026, 10, 22),
        "CLE",
        "PHI",
        "CLE",
        "Allen, Jarrett",
        "Questionable",
        "Injury/Illness - Right Patellar Tendinopathy",
    )
    # The first of the next day's games, which starts a new game date.
    tomorrow = next(row for row in rows if row.game_date == date(2026, 10, 23))
    assert (tomorrow.away, tomorrow.home, tomorrow.team, tomorrow.player) == (
        "CHI",
        "MIL",
        "CHI",
        "Awaka, Tobe",
    )


def test_a_reason_cut_by_a_page_break_is_joined_to_its_line() -> None:
    by_player = {row.player: row for row in parse(WEEKEND).rows}

    # "Return to Competition" ends page 3 and "Reconditioning" starts page 4.
    assert by_player["Plumlee, Mason"].reason == "Return to Competition Reconditioning"
    assert by_player["Demin, Egor"].reason == (
        "Injury/Illness - Left Plantar Fascia; Injury Management"
    )


def test_candidates_are_the_newest_quarter_hours_of_the_date_by_now() -> None:
    now = _eastern(2026, 10, 22, 13, 7)

    assert candidates(date(2026, 10, 22), now)[:3] == [
        _eastern(2026, 10, 22, 13, 0),
        _eastern(2026, 10, 22, 12, 45),
        _eastern(2026, 10, 22, 12, 30),
    ]
    assert len(candidates(date(2026, 10, 22), now)) == injury_pdf.DISCOVERY_TRIES
    assert candidates(date(2026, 10, 21), now)[0] == _eastern(2026, 10, 21, 23, 45)
    assert candidates(date(2026, 10, 23), now) == []
    # Just after midnight, only the day's first quarter hour has come.
    assert candidates(date(2026, 10, 22), _eastern(2026, 10, 22, 0, 10)) == [
        _eastern(2026, 10, 22, 0, 0)
    ]


def test_report_url_stamps_the_eastern_time_with_minutes() -> None:
    at = datetime(2026, 10, 22, 21, 0, tzinfo=UTC)

    assert report_url(at) == (
        "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-10-22_05_00PM.pdf"
    )


def _session(get: ScriptedGet, clock: FakeClock) -> NbaSession:
    return NbaSession(Connect(get), RateLimiter(1e6, clock), clock)


NOW = _eastern(2026, 10, 22, 17, 10)


def test_discover_falls_back_past_an_unpublished_report_and_caches_the_pdf(
    tmp_path: Path,
) -> None:
    clock = FakeClock(NOW)
    cache = RawCache(tmp_path, clock)
    get = ScriptedGet(fake_response(403), fake_response(200, WEEKDAY.read_bytes()))

    path = discover(date(2026, 10, 22), _session(get, clock), cache, NOW)

    assert [url for url, _ in get.calls] == [
        report_url(_eastern(2026, 10, 22, 17, 0)),
        report_url(_eastern(2026, 10, 22, 16, 45)),
    ]
    assert path == tmp_path / "raw" / "injury" / "report" / "2026-10-22T1645.pdf"
    assert path.read_bytes() == WEEKDAY.read_bytes()
    meta = cache.meta("injury", "report", "2026-10-22T1645", suffix=".pdf")
    assert meta is not None and meta["url"] == report_url(_eastern(2026, 10, 22, 16, 45))


def test_discover_reads_a_cached_report_without_a_request(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    cache = RawCache(tmp_path, clock)
    cache.put("injury", "report", "2026-10-22T1700", b"%PDF-", url="u", status=200, suffix=".pdf")
    get = ScriptedGet()

    path = discover(date(2026, 10, 22), _session(get, clock), cache, NOW)

    assert path.name == "2026-10-22T1700.pdf"
    assert get.calls == []


def test_discover_refuses_a_response_that_is_not_a_pdf(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    cache = RawCache(tmp_path, clock)
    get = ScriptedGet(fake_response(200, b"<html>"))

    with pytest.raises(NbaHttpError, match="is not a PDF"):
        discover(date(2026, 10, 22), _session(get, clock), cache, NOW)
    assert cache.list("injury", "report", suffix=".pdf") == []


def test_discover_raises_when_no_report_was_published(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    tries = injury_pdf.DISCOVERY_TRIES
    get = ScriptedGet(*(fake_response(403) for _ in range(tries)))

    with pytest.raises(InjuryReportNotFoundError, match="no injury report published on"):
        discover(date(2026, 10, 22), _session(get, clock), RawCache(tmp_path, clock), NOW)
    assert len(get.calls) == tries


def test_only_injury_pdf_imports_pdfplumber() -> None:
    importers = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if re.search(r"^\s*(import|from)\s+pdfplumber\b", path.read_text(), re.MULTILINE)
    ]

    assert importers == ["closinglineup/sources/injury_pdf.py"]
