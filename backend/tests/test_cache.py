import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from closinglineup.clock import FakeClock
from closinglineup.sources.cache import RawCache

URL = "https://stats.nba.com/stats/boxscoretraditionalv3"
NOW = datetime(2026, 10, 21, 3, 30, tzinfo=UTC)


@pytest.fixture
def cache(tmp_path: Path) -> RawCache:
    return RawCache(tmp_path, FakeClock(NOW))


def test_path_layout(cache: RawCache, tmp_path: Path) -> None:
    assert cache.path("nba_stats", "boxscoretraditionalv3", "0022500001") == (
        tmp_path / "raw" / "nba_stats" / "boxscoretraditionalv3" / "0022500001.json"
    )


def test_put_then_get_round_trips_the_bytes(cache: RawCache) -> None:
    assert not cache.exists("nba_stats", "box", "1")
    assert cache.get("nba_stats", "box", "1") is None

    assert cache.put("nba_stats", "box", "1", b'{"a": 1}', url=URL, status=200)

    assert cache.exists("nba_stats", "box", "1")
    assert cache.get("nba_stats", "box", "1") == b'{"a": 1}'


def test_second_put_without_force_leaves_the_file_unchanged(cache: RawCache) -> None:
    cache.put("nba_stats", "box", "1", b'{"v": 1}', url=URL, status=200)

    written = cache.put("nba_stats", "box", "1", b'{"v": 2}', url=URL, status=200)

    assert not written
    assert cache.get("nba_stats", "box", "1") == b'{"v": 1}'


def test_put_with_force_overwrites(cache: RawCache) -> None:
    cache.put("nba_stats", "box", "1", b'{"v": 1}', url=URL, status=200)

    assert cache.put("nba_stats", "box", "1", b'{"v": 2}', url=URL, status=200, force=True)

    assert cache.get("nba_stats", "box", "1") == b'{"v": 2}'


def test_meta_sidecar_records_fetched_at_status_and_params(cache: RawCache) -> None:
    params = {"GameID": "0022500001", "LeagueID": "00"}

    cache.put("nba_stats", "box", "0022500001", b"{}", url=URL, status=200, params=params)

    meta_path = cache.path("nba_stats", "box", "0022500001").with_suffix(".meta.json")
    assert json.loads(meta_path.read_text()) == {
        "fetched_at": "2026-10-21T03:30:00+00:00",
        "url": URL,
        "status": 200,
        "params": params,
    }


def test_list_returns_sorted_keys_without_sidecars(cache: RawCache) -> None:
    for key in ("0022500002", "0022500001"):
        cache.put("nba_stats", "box", key, b"{}", url=URL, status=200)
    cache.put("nba_stats", "other", "x", b"{}", url=URL, status=200)

    assert cache.list("nba_stats", "box") == ["0022500001", "0022500002"]
    assert cache.list("nba_stats", "missing") == []


def test_put_leaves_no_temp_files(cache: RawCache) -> None:
    cache.put("nba_stats", "box", "1", b"{}", url=URL, status=200)

    files = sorted(p.name for p in cache.path("nba_stats", "box", "1").parent.iterdir())
    assert files == ["1.json", "1.meta.json"]


def test_a_suffix_keeps_a_non_json_response_beside_its_json_sidecar(
    cache: RawCache, tmp_path: Path
) -> None:
    cache.put("injury", "report", "2026-10-22T1700", b"%PDF-", url=URL, status=200, suffix=".pdf")
    cache.put("injury", "report", "other", b"{}", url=URL, status=200)

    path = tmp_path / "raw" / "injury" / "report" / "2026-10-22T1700.pdf"
    assert cache.path("injury", "report", "2026-10-22T1700", suffix=".pdf") == path
    assert cache.get("injury", "report", "2026-10-22T1700", suffix=".pdf") == b"%PDF-"
    assert not cache.exists("injury", "report", "2026-10-22T1700")
    meta = cache.meta("injury", "report", "2026-10-22T1700", suffix=".pdf")
    assert meta is not None and meta["status"] == 200
    assert cache.list("injury", "report", suffix=".pdf") == ["2026-10-22T1700"]
