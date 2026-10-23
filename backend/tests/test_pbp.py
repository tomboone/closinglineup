import json
import re
from collections import Counter
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

import closinglineup
from closinglineup.parse import pbp
from closinglineup.sources import nba_cdn
from closinglineup.sources.cache import RawCache

FIXTURES = Path(__file__).parent / "fixtures" / "pbp"
# DEN at GSW, 137-131 after one overtime.
OVERTIME_GAME = "0022500006"
# MIN at OKC, with substitutions logged between free throws and an end-of-quarter heave.
QUIRK_GAME = "0022500064"
# OKC at SAS, in the 2026-27 feed: player-less actions have no `personId`, and a rebound is
# listed before its shot.
FEED_2026_27_GAME = "0022600003"
# ATL at NOP, which reconciliation matches to NBA.com's per-game possessions: clock overlap alone
# misses a steal and a foul logged before a substitution at a possession's start (NEU-1900).
RECONCILE_GAME = "0022500270"
GAMES = (OVERTIME_GAME, QUIRK_GAME, FEED_2026_27_GAME, RECONCILE_GAME)

GSW = 1610612744
# GSW's five for all of overtime in the overtime game.
GSW_OVERTIME_FIVE = [201143, 201939, 202710, 203110, 1630228]
DRAYMOND_GREEN = 203110

type Payload = dict[str, Any]


def _fixture(endpoint: str, game_id: str) -> Payload:
    return json.loads((FIXTURES / endpoint / f"{game_id}.json").read_text())


def _cache_pbp(cache: RawCache, game_id: str, payload: Payload) -> None:
    cache.put(
        nba_cdn.SOURCE,
        nba_cdn.PLAYBYPLAY,
        game_id,
        json.dumps(payload).encode(),
        url=f"{nba_cdn.CDN_BASE}/liveData/playbyplay/playbyplay_{game_id}.json",
        status=200,
    )


@pytest.fixture
def cache(tmp_path: Path) -> RawCache:
    cache = RawCache(tmp_path)
    for game_id in GAMES:
        _cache_pbp(cache, game_id, _fixture(nba_cdn.PLAYBYPLAY, game_id))
        pbp.materialize(cache, game_id)
    return cache


@pytest.fixture
def data_dir(cache: RawCache) -> Path:
    return cache.root.parent


def test_materialize_writes_the_live_feed_where_pbpstats_reads_it(cache: RawCache) -> None:
    path = pbp.materialize(cache, OVERTIME_GAME)

    assert path == cache.root.parent / "pbpstats" / "pbp" / f"live_{OVERTIME_GAME}.json"
    actions = json.loads(path.read_text())["game"]["actions"]
    raw = _fixture(nba_cdn.PLAYBYPLAY, OVERTIME_GAME)["game"]["actions"]
    assert actions == [a for a in raw if a["actionType"] != "game"]
    assert len(actions) == len(raw) - 1


def test_materialize_fills_in_person_id_and_restores_order_number_order(cache: RawCache) -> None:
    raw = _fixture(nba_cdn.PLAYBYPLAY, FEED_2026_27_GAME)["game"]["actions"]
    assert any("personId" not in a for a in raw)

    path = pbp.materialize(cache, FEED_2026_27_GAME)

    actions = json.loads(path.read_text())["game"]["actions"]
    assert all("personId" in a for a in actions)
    assert [a["personId"] for a in actions if a["actionType"] == "period"] == [0] * 8
    orders = [a["orderNumber"] for a in actions]
    assert orders == sorted(orders)
    numbers = [a["actionNumber"] for a in actions]
    # The rebound of a missed three with 21.1 seconds left in the third.
    assert numbers.index(598) < numbers.index(599)


def test_materialize_installs_the_committed_overrides(cache: RawCache) -> None:
    overrides = cache.root.parent / "pbpstats" / "overrides"

    assert sorted(p.name for p in overrides.iterdir()) == sorted(pbp.OVERRIDE_FILES)
    for name in pbp.OVERRIDE_FILES:
        assert (overrides / name).read_bytes() == (pbp.OVERRIDES_SOURCE / name).read_bytes()


def _commit_action_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> RawCache:
    """A cache holding the overtime game, materialized against `overrides` as the committed
    action overrides."""
    committed = tmp_path / "committed"
    committed.mkdir()
    (committed / "missing_period_starters.json").write_text("{}")
    (committed / pbp.ACTION_OVERRIDES).write_text(json.dumps(overrides))
    monkeypatch.setattr(pbp, "OVERRIDES_SOURCE", committed)
    cache = RawCache(tmp_path / "data")
    _cache_pbp(cache, OVERTIME_GAME, _fixture(nba_cdn.PLAYBYPLAY, OVERTIME_GAME))
    return cache


def _materialized_actions(path: Path) -> dict[int, Payload]:
    return {a["actionNumber"]: a for a in json.loads(path.read_text())["game"]["actions"]}


def test_an_action_override_changes_that_action_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _fixture(nba_cdn.PLAYBYPLAY, OVERTIME_GAME)["game"]["actions"]
    target = next(a for a in raw if a["actionType"] == "rebound")
    number = target["actionNumber"]
    overrides = {OVERTIME_GAME: {str(number): {"clock": "PT00M01.00S"}}}
    cache = _commit_action_overrides(tmp_path, monkeypatch, overrides)

    actions = _materialized_actions(pbp.materialize(cache, OVERTIME_GAME))

    expected = {a["actionNumber"]: a for a in raw if a["actionType"] != "game"}
    assert actions[number] == {**target, "clock": "PT00M01.00S"}
    assert {n: a for n, a in actions.items() if n != number} == {
        n: a for n, a in expected.items() if n != number
    }


def test_a_feed_with_no_action_override_passes_through_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overrides = {QUIRK_GAME: {"2": {"clock": "PT00M01.00S"}}}
    cache = _commit_action_overrides(tmp_path, monkeypatch, overrides)

    path = pbp.materialize(cache, OVERTIME_GAME)

    raw = _fixture(nba_cdn.PLAYBYPLAY, OVERTIME_GAME)["game"]["actions"]
    assert json.loads(path.read_text())["game"]["actions"] == [
        a for a in raw if a["actionType"] != "game"
    ]


def test_an_action_override_for_an_action_not_in_the_feed_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overrides = {OVERTIME_GAME: {"99999": {"clock": "PT00M01.00S"}}}
    cache = _commit_action_overrides(tmp_path, monkeypatch, overrides)

    with pytest.raises(pbp.PbpError, match="99999"):
        pbp.materialize(cache, OVERTIME_GAME)


def test_the_committed_action_overrides_name_known_fields() -> None:
    overrides = json.loads((pbp.OVERRIDES_SOURCE / pbp.ACTION_OVERRIDES).read_text())
    raw = _fixture(nba_cdn.PLAYBYPLAY, OVERTIME_GAME)["game"]["actions"]
    fields = {key for action in raw for key in action}

    assert overrides
    for game in overrides.values():
        for changes in game.values():
            assert changes
            assert set(changes) <= fields


def test_materialize_needs_the_cached_play_by_play(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=OVERTIME_GAME):
        pbp.materialize(RawCache(tmp_path), OVERTIME_GAME)


@pytest.mark.parametrize("game_id", GAMES)
def test_stint_points_sum_to_the_final_box_score(data_dir: Path, game_id: str) -> None:
    box = _fixture(nba_cdn.BOXSCORE, game_id)["game"]
    stints = pbp.stints_for_game(data_dir, game_id)

    pts_for: Counter[int] = Counter()
    pts_against: Counter[int] = Counter()
    for stint in stints:
        pts_for[stint.team_id] += stint.pts_for
        pts_against[stint.team_id] += stint.pts_against

    home, away = box["homeTeam"], box["awayTeam"]
    assert pts_for == {home["teamId"]: home["score"], away["teamId"]: away["score"]}
    assert pts_against == {home["teamId"]: away["score"], away["teamId"]: home["score"]}


@pytest.mark.parametrize("game_id", GAMES)
def test_stint_possessions_sum_to_pbpstats_counted_possessions(
    data_dir: Path, game_id: str
) -> None:
    stints = pbp.stints_for_game(data_dir, game_id)
    possessions = pbp.possessions_for_game(data_dir, game_id)

    by_stint: Counter[int] = Counter()
    for stint in stints:
        by_stint[stint.team_id] += stint.possessions
    counted = Counter(p.offense_team_id for p in possessions if p.counted)
    assert by_stint == counted
    assert set(counted) == {stint.team_id for stint in stints}


@pytest.mark.parametrize("game_id", GAMES)
def test_possessions_alternate_within_each_period(data_dir: Path, game_id: str) -> None:
    # Independent of how pbpstats counts: the ball changes hands, so in any period the two
    # teams' possessions differ by at most one.
    by_period: dict[int, Counter[int]] = {}
    for stint in pbp.stints_for_game(data_dir, game_id):
        by_period.setdefault(stint.period, Counter())[stint.team_id] += stint.possessions

    for counts in by_period.values():
        assert len(counts) == 2
        assert max(counts.values()) - min(counts.values()) <= 1


@pytest.mark.parametrize("game_id", GAMES)
def test_every_stint_is_five_on_five_over_elapsed_clock(data_dir: Path, game_id: str) -> None:
    stints = pbp.stints_for_game(data_dir, game_id)

    assert stints
    for stint in stints:
        for key in (stint.lineup_key, stint.opp_lineup_key):
            players = [int(p) for p in key.split("-")]
            assert len(players) == 5
            assert players == sorted(players)
        length = 720 if stint.period <= 4 else 300
        assert 0 <= stint.start_sec < stint.end_sec <= length


@pytest.mark.parametrize("game_id", GAMES)
def test_stints_cover_each_period_once_from_both_sides(data_dir: Path, game_id: str) -> None:
    stints = pbp.stints_for_game(data_dir, game_id)

    for team_id in {stint.team_id for stint in stints}:
        ours = [s for s in stints if s.team_id == team_id]
        for period in {s.period for s in ours}:
            spans = [(s.start_sec, s.end_sec) for s in ours if s.period == period]
            assert spans[0][0] == 0
            assert spans[-1][1] == (720 if period <= 4 else 300)
            assert all(a[1] == b[0] for a, b in pairwise(spans))
    views = {(s.period, s.start_sec, s.lineup_key, s.opp_lineup_key) for s in stints}
    assert len(views) == len(stints)
    for period, start, lineup, opp_lineup in views:
        assert (period, start, opp_lineup, lineup) in views


@pytest.mark.parametrize("game_id", GAMES)
def test_rows_for_game_reads_pbpstats_once_for_what_the_others_read(
    data_dir: Path, game_id: str
) -> None:
    rows = pbp.rows_for_game(data_dir, game_id)

    assert rows.stints == pbp.stints_for_game(data_dir, game_id)
    assert rows.possessions == pbp.possessions_for_game(data_dir, game_id)


@pytest.mark.parametrize("game_id", GAMES)
def test_every_possession_has_a_five_of_each_team_but_those_nba_com_does_not_count(
    data_dir: Path, game_id: str
) -> None:
    rows = pbp.rows_for_game(data_dir, game_id)

    teams = {stint.team_id for stint in rows.stints}
    credited: dict[tuple[int, int], set[int]] = {}
    for row in rows.possession_lineups:
        credited.setdefault((row.period, row.number), set()).add(row.team_id)
        assert len(row.lineup_key.split("-")) == 5
    uncounted = 0
    for p in rows.possessions:
        instant = p.start_sec == p.end_sec
        if instant and (
            (p.end_type == "EndOfPeriod" and not p.counted) or p.end_type == "Substitution"
        ):
            uncounted += 1
            assert (p.period, p.number) not in credited
        else:
            assert credited[(p.period, p.number)] == teams
    # A possession a substitution splits has a row for each team's five on either side of it.
    assert len(rows.possession_lineups) > 2 * (len(rows.possessions) - uncounted)


def test_overtime_is_a_fifth_period(data_dir: Path) -> None:
    overtime = [s for s in pbp.stints_for_game(data_dir, OVERTIME_GAME) if s.period == 5]

    gsw = {s.lineup_key for s in overtime if s.team_id == GSW}
    assert gsw == {"-".join(str(p) for p in GSW_OVERTIME_FIVE)}


def test_possessions_carry_the_game_and_run_in_order(data_dir: Path) -> None:
    possessions = pbp.possessions_for_game(data_dir, OVERTIME_GAME)

    assert len(possessions) == 213
    assert {p.game_id for p in possessions} == {OVERTIME_GAME}
    assert [p.period for p in possessions] == sorted(p.period for p in possessions)
    first = possessions[0]
    assert (first.period, first.number, first.start_sec) == (1, 1, 0)
    assert all(p.start_sec <= p.end_sec for p in possessions)


def test_a_possession_after_a_heave_rebound_has_no_start_type(data_dir: Path) -> None:
    possessions = pbp.possessions_for_game(data_dir, QUIRK_GAME)

    untyped = [p for p in possessions if p.start_type is None]
    # MIN's heave at the end of the first quarter, rebounded with 2.0 seconds left.
    assert [(p.period, p.start_sec) for p in untyped] == [(1, 718.0)]
    assert all(p.start_type for p in possessions if p is not untyped[0])


def test_possessions_say_how_they_ended(data_dir: Path) -> None:
    possessions = pbp.possessions_for_game(data_dir, OVERTIME_GAME)

    ends = Counter(p.end_type for p in possessions)
    assert {"FieldGoal", "FreeThrow", "Rebound", "Turnover", "EndOfPeriod"} <= set(ends)
    # Every period's last possession ends on its period-end event, once `game` actions are gone.
    assert ends["EndOfPeriod"] == 5


def _without_player_in_overtime(player_id: int) -> Payload:
    """The overtime game with every overtime event naming `player_id` removed, so pbpstats can
    no longer tell that he started overtime."""
    payload = _fixture(nba_cdn.PLAYBYPLAY, OVERTIME_GAME)
    game = payload["game"]
    actions = [
        a
        for a in game["actions"]
        if not (a["period"] == 5 and player_id in a.get("personIdsFilter", []))
    ]
    return {**payload, "game": {**game, "actions": actions}}


def test_missing_period_starters_fail_without_an_override(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    _cache_pbp(cache, OVERTIME_GAME, _without_player_in_overtime(DRAYMOND_GREEN))
    pbp.materialize(cache, OVERTIME_GAME)

    with pytest.raises(pbp.PbpError, match="Period: 5"):
        pbp.stints_for_game(tmp_path, OVERTIME_GAME)


def test_missing_period_starters_override_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed = tmp_path / "committed"
    committed.mkdir()
    override = {OVERTIME_GAME: {"5": {str(GSW): GSW_OVERTIME_FIVE}}}
    (committed / "missing_period_starters.json").write_text(json.dumps(override))
    (committed / pbp.ACTION_OVERRIDES).write_text("{}")
    monkeypatch.setattr(pbp, "OVERRIDES_SOURCE", committed)
    data_dir = tmp_path / "data"
    cache = RawCache(data_dir)
    _cache_pbp(cache, OVERTIME_GAME, _without_player_in_overtime(DRAYMOND_GREEN))
    pbp.materialize(cache, OVERTIME_GAME)

    overtime = [s for s in pbp.stints_for_game(data_dir, OVERTIME_GAME) if s.period == 5]

    assert overtime
    assert all(str(DRAYMOND_GREEN) in s.lineup_key for s in overtime if s.team_id == GSW)


def test_a_missing_materialized_game_raises_pbp_error(tmp_path: Path) -> None:
    with pytest.raises(pbp.PbpError, match=OVERTIME_GAME):
        pbp.stints_for_game(tmp_path, OVERTIME_GAME)


def test_parse_pbp_is_the_only_importer_of_pbpstats() -> None:
    package = Path(closinglineup.__file__).parent
    importers = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if re.search(r"^\s*(import|from)\s+pbpstats\b", path.read_text(), re.MULTILINE)
    }

    assert importers == {"parse/pbp.py"}
