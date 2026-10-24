import json
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from closinglineup.clock import FakeClock
from closinglineup.season import espn_season
from closinglineup.sources.cache import RawCache
from closinglineup.sources.espn_injuries import (
    URL,
    EspnInjuriesError,
    EspnInjuryRow,
    fetch,
    parse,
)

FIXTURES = Path(__file__).parent / "fixtures" / "espn"
REPORT = FIXTURES / "injuries" / "20261023T144550Z.json"
FETCHED = datetime(2026, 10, 23, 14, 46, 3, tzinfo=UTC)


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(data))
    return path


def _report(*injuries: dict[str, Any], group: str = "13") -> dict[str, Any]:
    return {
        "timestamp": "2026-10-23T14:45:50Z",
        "injuries": [{"id": group, "injuries": list(injuries)}],
    }


def _injury(status: str, team: str | None = "13", **details: str) -> dict[str, Any]:
    athlete: dict[str, Any] = {"displayName": "LeBron James"}
    if team is not None:
        athlete["team"] = {"id": team}
    return {"status": status, "athlete": athlete, "details": details}


@pytest.mark.parametrize(("day", "season"), [(date(2026, 4, 12), 2026), (date(2026, 10, 23), 2027)])
def test_espn_season_is_the_year_the_season_ends(day: date, season: int) -> None:
    assert espn_season(day) == season


def test_the_fixture_parses_every_line_with_statuses_mapped() -> None:
    report = parse(REPORT)

    # Counted by hand from the fixture's `status` fields: 50 Out, 29 Day-To-Day, 25 teams.
    assert report.report_datetime == datetime(2026, 10, 23, 14, 45, 50, tzinfo=UTC)
    assert len(report.rows) == 79
    assert Counter(row.status for row in report.rows) == {"Out": 50, "Questionable": 29}
    assert len({row.team for row in report.rows}) == 25
    assert report.unmapped == []
    assert report.rows[0] == EspnInjuryRow("ATL", "Mouhamed Gueye", "Out", "Left Foot; Fracture")


def test_a_line_goes_to_the_athletes_team_over_its_groups() -> None:
    report = parse(REPORT)

    # Listed under the LA Clippers; ESPN's own team for him is Toronto.
    assert [row.team for row in report.rows if row.player == "Brandon Ingram"] == ["TOR"]


def test_a_line_with_no_athlete_team_goes_to_its_group(tmp_path: Path) -> None:
    report = parse(_write(tmp_path, _report(_injury("Out", team=None), group="2")))

    assert [row.team for row in report.rows] == ["BOS"]


@pytest.mark.parametrize(
    ("status", "mapped"), [("Out", "Out"), ("Day-To-Day", "Questionable"), ("GTD", "Questionable")]
)
def test_espn_statuses_map_onto_the_official_reports(
    tmp_path: Path, status: str, mapped: str
) -> None:
    report = parse(_write(tmp_path, _report(_injury(status))))

    assert [row.status for row in report.rows] == [mapped]


def test_a_status_with_no_mapping_is_listed_and_has_no_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    report = parse(_write(tmp_path, _report(_injury("Suspension"), _injury("Out"))))

    assert [row.status for row in report.rows] == ["Out"]
    assert report.unmapped == ["LeBron James (LAL): Suspension"]
    assert "ESPN injury status not mapped" in caplog.text


@pytest.mark.parametrize(
    ("details", "reason"),
    [
        ({"side": "Left", "type": "Foot", "detail": "Fracture"}, "Left Foot; Fracture"),
        ({"side": "Not Specified", "type": "Illness", "detail": "Not Specified"}, "Illness"),
        ({"type": "Concussion", "detail": "Concussion"}, "Concussion"),
        ({}, None),
    ],
)
def test_the_reason_reads_from_the_details_leaving_out_what_espn_does_not_know(
    tmp_path: Path, details: dict[str, str], reason: str | None
) -> None:
    report = parse(_write(tmp_path, _report(_injury("Out", **details))))

    assert [row.reason for row in report.rows] == [reason]


@pytest.mark.parametrize(
    "data",
    [
        {"injuries": []},
        _report(_injury("Out", team="99")),
        _report({"status": "Out", "athlete": {"team": {"id": "13"}}}),
    ],
    ids=["no timestamp", "unknown team", "no athlete name"],
)
def test_a_report_that_does_not_read_raises(tmp_path: Path, data: dict[str, Any]) -> None:
    with pytest.raises(EspnInjuriesError):
        parse(_write(tmp_path, data))


@respx.mock
def test_fetch_sends_the_season_and_caches_the_response_by_the_instant_fetched(
    tmp_path: Path,
) -> None:
    route = respx.get(URL).respond(200, content=REPORT.read_bytes())
    cache = RawCache(tmp_path, FakeClock(FETCHED))

    with httpx.Client() as client:
        path = fetch(client, cache, date(2026, 10, 23))

    assert dict(route.calls.last.request.url.params) == {"season": "2027", "seasontype": "2"}
    assert path == tmp_path / "raw" / "espn" / "injuries" / "20261023T144603Z.json"
    assert path.read_bytes() == REPORT.read_bytes()


@pytest.mark.parametrize(
    "response",
    [httpx.Response(503), httpx.Response(200, content=b"<html>"), httpx.Response(200, json=[])],
    ids=["error status", "not JSON", "not an object"],
)
@respx.mock
def test_a_bad_response_raises_and_is_not_cached(tmp_path: Path, response: httpx.Response) -> None:
    respx.get(URL).mock(return_value=response)

    with httpx.Client() as client, pytest.raises((httpx.HTTPStatusError, EspnInjuriesError)):
        fetch(client, RawCache(tmp_path, FakeClock(FETCHED)), date(2026, 10, 23))

    assert not (tmp_path / "raw").exists()
