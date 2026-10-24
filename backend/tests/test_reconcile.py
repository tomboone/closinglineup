import json
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from closinglineup.cli import app
from closinglineup.db.models import Game, Possession, PossessionLineup, Stint
from closinglineup.derive import reconcile
from closinglineup.derive.reconcile import (
    Check,
    NbaRatings,
    Tally,
    Totals,
    compare,
    describe,
    lineup_tolerance,
    nba_lineups,
    nba_on_court,
    sample_teams,
    season_rows,
    season_type_of,
    tallies,
    top_games,
)
from closinglineup.parse import pbp
from closinglineup.parse.rows import PossessionLineupRow, PossessionRow, StintRow
from closinglineup.sources import nba_cdn, nba_stats
from closinglineup.sources.cache import RawCache
from closinglineup.sources.nba_stats import SeasonType
from closinglineup.teams import TEAMS
from tests.test_nba_stats import season_fixture_body
from tests.test_pbp import FIXTURES as PBP_FIXTURES
from tests.test_pbp import (
    GAMES,
    GSW,
    GSW_OVERTIME_FIVE,
    OVERTIME_GAME,
    QUIRK_GAME,
    RECONCILE_GAME,
)

runner = CliRunner()

type Payload = dict[str, Any]

DEN = 1610612743
OVERTIME_FIVE = "-".join(str(p) for p in GSW_OVERTIME_FIVE)
# NBA.com's `leaguedashlineups` and `leagueplayerondetails` for each team of each of these games,
# on its date alone.
RECORDED_GAMES = (OVERTIME_GAME, QUIRK_GAME, RECONCILE_GAME)
RECORDED = Path(__file__).parent / "fixtures" / "reconcile"


@pytest.fixture(scope="module")
def fixture_rows(tmp_path_factory: pytest.TempPathFactory) -> dict[str, pbp.GameRows]:
    """Every fixture game's rows, as `parse.pbp` emits them."""
    cache = RawCache(tmp_path_factory.mktemp("data"))
    rows: dict[str, pbp.GameRows] = {}
    for game_id in GAMES:
        body = (PBP_FIXTURES / nba_cdn.PLAYBYPLAY / f"{game_id}.json").read_bytes()
        cache.put(nba_cdn.SOURCE, nba_cdn.PLAYBYPLAY, game_id, body, url="u", status=200)
        pbp.materialize(cache, game_id)
        rows[game_id] = pbp.rows_for_game(cache.root.parent, game_id)
    return rows


def _tallies(
    rows: pbp.GameRows,
) -> tuple[dict[tuple[int, str], Tally], dict[tuple[int, str], Tally]]:
    return tallies(rows.stints, rows.possessions, rows.possession_lineups)


def test_the_overtime_fives_totals_match_the_hand_count(
    fixture_rows: dict[str, pbp.GameRows],
) -> None:
    lineups, _ = _tallies(fixture_rows[OVERTIME_GAME])

    tally = lineups[(GSW, OVERTIME_FIVE)]
    # Counted by hand from the play-by-play. 3:50 to 0:25.7 of the fourth, 109-116 to 117-120:
    # GSW's shot-clock turnover, Curry's two trips to the line, his three, his missed layup, and
    # the possession after Gordon's three at 0:26, which the five starts before GSW's timeout
    # and substitutions at 0:25.7, are six possessions; DEN's two misses, its trip to the line,
    # its miss and Gordon's three are five. All of overtime, 120-120 to 137-131: GSW's eleven
    # possessions, the last running out the clock, and DEN's ten.
    assert tally.by_game == {OVERTIME_GAME: Totals(17, 15, 25, 15)}
    assert tally.total.off_rating == pytest.approx(25 / 17 * 100)
    assert tally.total.def_rating == pytest.approx(100.0)


def _recorded(game_id: str, endpoint: str) -> dict[tuple[int, str], NbaRatings]:
    """NBA.com's rows for the game, from its `{team_id: response}` fixture."""
    payloads: dict[str, Payload] = json.loads((RECORDED / f"{game_id}_{endpoint}.json").read_text())
    read = nba_lineups if endpoint == nba_stats.LEAGUE_DASH_LINEUPS else nba_on_court
    return {(n.team_id, n.key): n for p in payloads.values() for n in read(p)}


@pytest.mark.parametrize("game_id", RECORDED_GAMES)
def test_every_five_has_nba_coms_possessions_and_ratings_in_the_recorded_games(
    fixture_rows: dict[str, pbp.GameRows], game_id: str
) -> None:
    lineups, _ = _tallies(fixture_rows[game_id])
    nba = _recorded(game_id, nba_stats.LEAGUE_DASH_LINEUPS)

    ours = {key: lineups.get(key, Tally()).total for key in nba}

    assert {key: t.possessions for key, t in ours.items()} == {
        key: n.possessions for key, n in nba.items()
    }
    # Both ways, so the stints' points and the opponent's possessions too: a free throw's
    # points go to the five on the court at its foul, as NBA.com credits them.
    for key, n in nba.items():
        assert (ours[key].off_rating or 0.0) == pytest.approx(n.off_rating, abs=0.05), key
        assert (ours[key].def_rating or 0.0) == pytest.approx(n.def_rating, abs=0.05), key


@pytest.mark.parametrize("game_id", RECORDED_GAMES)
def test_every_player_has_nba_coms_on_court_possessions_in_the_recorded_games(
    fixture_rows: dict[str, pbp.GameRows], game_id: str
) -> None:
    _, players = _tallies(fixture_rows[game_id])
    nba = _recorded(game_id, nba_stats.LEAGUE_PLAYER_ON_DETAILS)

    assert {key: players[key].total.possessions for key in nba} == {
        key: n.possessions for key, n in nba.items()
    }


def test_a_possession_counts_once_for_each_five_it_lists_and_each_player_in_any() -> None:
    def stint(start: float, end: float, team_id: int, key: str, pf: int, pa: int) -> StintRow:
        return StintRow(
            game_id=OVERTIME_GAME,
            period=1,
            start_sec=start,
            end_sec=end,
            team_id=team_id,
            lineup_key=key,
            opp_lineup_key="",
            possessions=0,
            pts_for=pf,
            pts_against=pa,
        )

    def possession(number: int, offense: int) -> PossessionRow:
        return PossessionRow(
            game_id=OVERTIME_GAME,
            period=1,
            number=number,
            offense_team_id=offense,
            start_sec=0,
            end_sec=0,
            start_score_margin=0,
            has_timeout=False,
            previous_has_timeout=False,
            end_type="FieldGoal",
            counted=True,
        )

    def credited(number: int, team_id: int, *keys: str) -> list[PossessionLineupRow]:
        return [
            PossessionLineupRow(
                game_id=OVERTIME_GAME, period=1, number=number, team_id=team_id, lineup_key=key
            )
            for key in keys
        ]

    # GSW's 1-5 then 1-4 and 6, against one DEN five throughout.
    first, second, den = "1-2-3-4-5", "1-2-3-4-6", "11-12-13-14-15"
    stints = [
        stint(0, 10, GSW, first, 2, 0),
        stint(0, 10, DEN, den, 0, 2),
        stint(10, 20, GSW, second, 0, 3),
        stint(10, 20, DEN, den, 3, 0),
    ]
    possessions = [possession(1, GSW), possession(2, GSW), possession(3, DEN), possession(4, GSW)]
    lineups = [
        *credited(1, GSW, first),
        *credited(1, DEN, den),
        # Split by the substitution.
        *credited(2, GSW, first, second),
        *credited(2, DEN, den),
        *credited(3, GSW, second),
        *credited(3, DEN, den),
        # Possession 4 is one NBA.com does not count, so it has no fives at all.
    ]

    lineup_totals, players = tallies(stints, possessions, lineups)

    assert lineup_totals[(GSW, first)].total == Totals(2, 0, 2, 0)
    assert lineup_totals[(GSW, second)].total == Totals(1, 1, 0, 3)
    assert lineup_totals[(DEN, den)].total == Totals(1, 2, 3, 2)
    # Possession 2 counts once for the four who played both sides of the substitution.
    assert players[(GSW, "1")].total == Totals(2, 1, 2, 3)
    assert players[(GSW, "5")].total == Totals(2, 0, 2, 0)
    assert players[(GSW, "6")].total == Totals(1, 1, 0, 3)


def test_lineup_tolerance_is_three_possessions_worth_of_net_rating() -> None:
    assert lineup_tolerance(_nba("a", 0.0, possessions=110)) == pytest.approx(3.0)
    assert lineup_tolerance(_nba("a", 0.0, possessions=1100)) == pytest.approx(0.3)


def test_nba_lineups_keys_each_group_as_parse_pbp_keys_a_lineup() -> None:
    payload = json.loads(season_fixture_body(nba_stats.LEAGUE_DASH_LINEUPS))

    lineups = nba_lineups(payload)

    assert len(lineups) == 10
    assert lineups[0] == NbaRatings(
        team_id=1610612750,
        key="203497-203944-1628978-1630162-1630183",
        name="R. Gobert - J. Randle - D. DiVincenzo - A. Edwards - J. McDaniels",
        games=54,
        possessions=1502,
        off_rating=118.8,
        def_rating=110.9,
        net_rating=7.9,
    )


def test_nba_on_court_reads_each_players_on_row() -> None:
    payload = json.loads(season_fixture_body(nba_stats.LEAGUE_PLAYER_ON_DETAILS))

    on_court = {n.key: n for n in nba_on_court(payload)}

    assert len(on_court) == 22
    curry = on_court["201939"]
    assert (curry.team_id, curry.name, curry.games, curry.possessions) == (
        GSW,
        "Curry, Stephen",
        43,
        2873,
    )
    assert (curry.off_rating, curry.def_rating, curry.net_rating) == (117.6, 114.8, 2.8)


def _nba(key: str, net: float, possessions: int = 200, team_id: int = GSW) -> NbaRatings:
    return NbaRatings(team_id, key, key, 2, possessions, 100.0 + net, 100.0, net)


def _tally(*games: tuple[str, Totals]) -> Tally:
    tally = Tally()
    for game_id, totals in games:
        tally.add(game_id, totals)
    return tally


def _within(tolerance: float) -> Callable[[NbaRatings], float]:
    return lambda nba: tolerance


def test_compare_checks_only_rows_with_enough_possessions_most_first() -> None:
    # 110 for, 100 against over 100 possessions each way: net 10.0.
    ours = {(GSW, "a"): _tally(("g1", Totals(100, 100, 110, 100)))}
    theirs = [_nba("a", 10.1), _nba("b", 3.0, possessions=99), _nba("c", 0.0, possessions=300)]

    checks = compare("lineup", ours, theirs, tolerance=_within(0.1))

    assert [c.nba.key for c in checks] == ["c", "a"]
    c, a = checks
    # A lineup we have no stints for is outside, with no games to name.
    assert (c.diff, c.within, c.games, c.top_games) == (None, False, 0, ())
    assert a.diff == pytest.approx(-0.1)
    assert (a.within, a.games, a.top_games) == (True, 1, ())


def test_a_row_outside_tolerance_names_the_games_that_pull_it_furthest() -> None:
    ours = {
        (GSW, "a"): _tally(
            ("g1", Totals(50, 50, 50, 50)),
            ("g2", Totals(50, 50, 50, 50)),
            # A game that reads 40 points better than the rest.
            ("g3", Totals(10, 10, 30, 10)),
        )
    }

    [check] = compare("lineup", ours, [_nba("a", 0.0)], tolerance=_within(0.1))

    assert not check.within
    # Taking out g3 closes the gap; taking out g1 or g2 widens it.
    assert check.top_games == ("g3",)


def test_top_games_ranks_by_how_much_closer_each_removal_brings_the_net_rating() -> None:
    tally = _tally(
        ("g1", Totals(50, 50, 50, 50)),
        ("g2", Totals(10, 10, 20, 10)),
        ("g3", Totals(10, 10, 30, 10)),
    )

    assert top_games(tally, 0.0) == ("g3", "g2")
    assert top_games(tally, 0.0, limit=1) == ("g3",)
    # The only game takes the whole gap with it.
    assert top_games(_tally(("g1", Totals(10, 10, 30, 10))), 0.0) == ("g1",)
    assert top_games(Tally(), 0.0) == ()


def test_describe_puts_our_numbers_beside_nba_coms() -> None:
    check = Check(
        "lineup",
        _nba("1-2-3-4-5", 3.0),
        Totals(100, 100, 110, 100),
        games=3,
        tolerance=0.1,
        top_games=("g3", "g1"),
    )

    assert describe(check) == (
        "GSW lineup 1-2-3-4-5 (1-2-3-4-5): net 10.0 vs 3.0 (+7.00, tolerance ±0.10), "
        "ortg 110.0 vs 103.0, "
        "drtg 100.0 vs 100.0, poss 100 vs 200, games 3 vs 2; top games g3, g1"
    )


def test_sample_teams_moves_on_two_teams_a_day_and_covers_the_league_in_15_days() -> None:
    first = date(2026, 10, 20)

    samples = [sample_teams(first + timedelta(days=n)) for n in range(15)]

    assert all(len(set(sample)) == 2 for sample in samples)
    assert sorted(t for sample in samples for t in sample) == sorted(t.nba_team_id for t in TEAMS)
    assert sample_teams(first + timedelta(days=15)) == samples[0]


@pytest.mark.parametrize(
    ("game_id", "season_type"),
    [
        ("0022500006", SeasonType.REGULAR_SEASON),
        ("0042500101", SeasonType.PLAYOFFS),
        ("0052500101", SeasonType.PLAY_IN),
        ("0062500001", None),
    ],
)
def test_season_type_of_reads_the_game_ids_season_type(
    game_id: str, season_type: SeasonType | None
) -> None:
    assert season_type_of(game_id) == season_type


# A season no other test loads, and player IDs no real player has.
SEASON = "2017-18"
REGULAR_GAME = "0021700901"
PLAYOFF_GAME = "0041700101"
GSW_FIVE = "9100001-9100002-9100003-9100004-9100005"
DEN_FIVE = "9100011-9100012-9100013-9100014-9100015"
DEN_OTHER_FIVE = "9100011-9100012-9100013-9100014-9100016"


def _stint_row(
    game_id: str,
    start_sec: float,
    team_id: int,
    key: str,
    opp_key: str,
    poss: int,
    pf: int,
    pa: int,
) -> Stint:
    return Stint(
        game_id=game_id,
        period=1,
        start_sec=start_sec,
        end_sec=start_sec + 60,
        team_id=team_id,
        lineup_key=key,
        opp_lineup_key=opp_key,
        possessions=poss,
        pts_for=pf,
        pts_against=pa,
    )


def _possession_row(game_id: str, number: int, offense: int, start_sec: float) -> Possession:
    return Possession(
        game_id=game_id,
        period=1,
        number=number,
        offense_team_id=offense,
        start_sec=start_sec,
        end_sec=start_sec + 4,
        start_score_margin=0,
        start_type=None,
        has_timeout=False,
        previous_has_timeout=False,
        end_type="FieldGoal",
        counted=True,
    )


def _possession_lineup(game_id: str, number: int, team_id: int, key: str) -> PossessionLineup:
    return PossessionLineup(
        game_id=game_id, period=1, number=number, team_id=team_id, lineup_key=key
    )


def _clear(db: sessionmaker[Session]) -> None:
    games = (REGULAR_GAME, PLAYOFF_GAME)
    with db.begin() as session:
        session.execute(delete(PossessionLineup).where(PossessionLineup.game_id.in_(games)))
        session.execute(delete(Possession).where(Possession.game_id.in_(games)))
        session.execute(delete(Stint).where(Stint.game_id.in_(games)))
        session.execute(delete(Game).where(Game.game_id.in_(games)))


@pytest.fixture
def stints(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """GSW's five against two DEN fives in a regular-season game, with 15 GSW and 14 DEN
    possessions, and against one in a playoff game, with a possession each."""
    _clear(db)
    with db.begin() as session:
        for game_id, phase, day in (
            (REGULAR_GAME, "regular", date(2017, 10, 20)),
            (PLAYOFF_GAME, "playoff", date(2018, 4, 20)),
        ):
            session.add(
                Game(
                    game_id=game_id,
                    season=SEASON,
                    game_date=day,
                    phase=phase,
                    home_team_id=GSW,
                    away_team_id=DEN,
                    status="final",
                    home_b2b=False,
                    away_b2b=False,
                )
            )
        session.flush()
        session.add_all(
            [
                _stint_row(REGULAR_GAME, 0.0, GSW, GSW_FIVE, DEN_FIVE, 10, 12, 8),
                _stint_row(REGULAR_GAME, 0.0, DEN, DEN_FIVE, GSW_FIVE, 9, 8, 12),
                _stint_row(REGULAR_GAME, 60.0, GSW, GSW_FIVE, DEN_OTHER_FIVE, 5, 4, 6),
                _stint_row(REGULAR_GAME, 60.0, DEN, DEN_OTHER_FIVE, GSW_FIVE, 5, 6, 4),
                _stint_row(PLAYOFF_GAME, 0.0, GSW, GSW_FIVE, DEN_FIVE, 50, 60, 40),
                _stint_row(PLAYOFF_GAME, 0.0, DEN, DEN_FIVE, GSW_FIVE, 50, 40, 60),
                # Alternating from GSW, 4 s each; none spans the substitution at 60 s.
                *(
                    _possession_row(REGULAR_GAME, n + 1, GSW if n % 2 == 0 else DEN, 4.0 * n)
                    for n in range(29)
                ),
                _possession_row(PLAYOFF_GAME, 1, GSW, 0.0),
                _possession_row(PLAYOFF_GAME, 2, DEN, 4.0),
            ]
        )
        session.flush()
        session.add_all(
            [
                *(_possession_lineup(REGULAR_GAME, n + 1, GSW, GSW_FIVE) for n in range(29)),
                *(
                    _possession_lineup(
                        REGULAR_GAME, n + 1, DEN, DEN_FIVE if n < 15 else DEN_OTHER_FIVE
                    )
                    for n in range(29)
                ),
                *(
                    _possession_lineup(PLAYOFF_GAME, number, team_id, key)
                    for number in (1, 2)
                    for team_id, key in ((GSW, GSW_FIVE), (DEN, DEN_FIVE))
                ),
            ]
        )
    try:
        yield db
    finally:
        _clear(db)


def test_season_rows_reads_both_teams_rows_of_the_season_types_games(
    stints: sessionmaker[Session],
) -> None:
    found, possessions, possession_lineups = season_rows(
        stints, SEASON, SeasonType.REGULAR_SEASON, [GSW]
    )

    assert sorted((s.team_id, s.start_sec) for s in found) == [
        (DEN, 0.0),
        (DEN, 60.0),
        (GSW, 0.0),
        (GSW, 60.0),
    ]
    assert len(possessions) == 29
    assert {p.game_id for p in possessions} == {REGULAR_GAME}
    assert len(possession_lineups) == 58
    assert {p.game_id for p in possession_lineups} == {REGULAR_GAME}
    playoff_stints, playoff_possessions, playoff_lineups = season_rows(
        stints, SEASON, SeasonType.PLAYOFFS, [DEN]
    )
    assert {s.game_id for s in playoff_stints} == {PLAYOFF_GAME}
    assert [p.number for p in sorted(playoff_possessions, key=lambda p: p.number)] == [1, 2]
    assert {p.game_id for p in playoff_lineups} == {PLAYOFF_GAME}


def _result_set(endpoint: str, rows: list[dict[str, Any]]) -> bytes:
    """The endpoint's fixture with `rows` in place of its own, every other column zero."""
    payload: Payload = json.loads(season_fixture_body(endpoint))
    result_set = payload["resultSets"][0]
    headers: list[str] = result_set["headers"]
    result_set["rowSet"] = [[row.get(h, 0) for h in headers] for row in rows]
    return json.dumps(payload).encode()


def _ratings(net: float, poss: int) -> dict[str, Any]:
    return {
        "TEAM_ID": GSW,
        "GP": 1,
        "POSS": poss,
        "OFF_RATING": 106.7,
        "DEF_RATING": 100.0,
        "NET_RATING": net,
    }


def cache_nba(cache: RawCache, *, second_net: float) -> None:
    """NBA.com's 2017-18 lineups and GSW on/off: GSW's five as we have it (16 for, 14 against
    over 15 and 14 possessions: net 6.7) and a DEN five; GSW's first player as we have him, the
    second at `second_net`."""
    season_type = SeasonType.REGULAR_SEASON
    lineups = _result_set(
        nba_stats.LEAGUE_DASH_LINEUPS,
        [
            {"GROUP_ID": f"-{GSW_FIVE}-", "GROUP_NAME": "GSW five", **_ratings(6.7, 15)},
            {"GROUP_ID": f"-{DEN_FIVE}-", "GROUP_NAME": "DEN five", **_ratings(0.0, 900)}
            | {"TEAM_ID": DEN},
        ],
    )
    on_off = _result_set(
        nba_stats.LEAGUE_PLAYER_ON_DETAILS,
        [
            {"VS_PLAYER_ID": 9100001, "VS_PLAYER_NAME": "One", "COURT_STATUS": "On"}
            | _ratings(6.7, 15),
            {"VS_PLAYER_ID": 9100002, "VS_PLAYER_NAME": "Two", "COURT_STATUS": "On"}
            | _ratings(second_net, 15),
        ],
    )
    for endpoint, key, body in (
        (
            nba_stats.LEAGUE_DASH_LINEUPS,
            nba_stats.league_dash_lineups_key(SEASON, season_type),
            lineups,
        ),
        (
            nba_stats.LEAGUE_PLAYER_ON_DETAILS,
            nba_stats.league_player_on_details_key(GSW, SEASON, season_type),
            on_off,
        ),
    ):
        cache.put(nba_stats.SOURCE, endpoint, key, body, url="u", status=200)


def test_reconcile_compares_the_teams_lineups_and_players_with_nba_coms(
    stints: sessionmaker[Session], tmp_path: Path
) -> None:
    cache = RawCache(tmp_path)
    cache_nba(cache, second_net=9.0)

    report = reconcile.reconcile(
        stints, cache, SEASON, SeasonType.REGULAR_SEASON, [GSW], min_possessions=10
    )

    # The DEN five is not GSW's, and the playoff game is not regular season.
    [lineup] = report.lineups
    assert (lineup.nba.key, lineup.ours, lineup.games) == (GSW_FIVE, Totals(15, 14, 16, 14), 1)
    assert lineup.within
    one, two = report.on_court
    assert one.within
    assert (two.within, two.top_games) == (False, (REGULAR_GAME,))
    assert report.outside == [two]


def test_reconcile_without_nba_coms_lineups_cached_raises(
    stints: sessionmaker[Session], tmp_path: Path
) -> None:
    with pytest.raises(reconcile.MissingResponseError, match="leaguedashlineups 2017-18_regular"):
        reconcile.reconcile(stints, RawCache(tmp_path), SEASON, SeasonType.REGULAR_SEASON, [GSW])


@pytest.fixture
def reconcile_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_url: str) -> RawCache:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return RawCache(tmp_path)


def test_reconcile_command_lists_rows_outside_tolerance_and_exits_1(
    stints: sessionmaker[Session], reconcile_env: RawCache
) -> None:
    cache_nba(reconcile_env, second_net=9.0)
    args = ["reconcile", "--season", SEASON, "--teams", "gsw", "--min-possessions", "10"]

    result = runner.invoke(app, args)

    assert result.exit_code == 1, result.output
    assert result.output.splitlines() == [
        "OUTSIDE GSW on_court 9100002 (Two): net 6.7 vs 9.0 (-2.33, tolerance ±0.50), "
        "ortg 106.7 vs 106.7, drtg 100.0 vs 100.0, poss 15 vs 15, games 1 vs 1; "
        f"top games {REGULAR_GAME}",
        "reconcile 2017-18 regular-season: 1 lineups, 0 outside 3 possessions' worth; "
        "2 players on court, 1 outside ±0.5",
    ]


def test_reconcile_command_within_tolerance_exits_0_and_all_lists_every_row(
    stints: sessionmaker[Session], reconcile_env: RawCache
) -> None:
    cache_nba(reconcile_env, second_net=6.7)
    args = ["reconcile", "--season", SEASON, "--teams", "GSW", "--min-possessions", "10", "--all"]

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert [line.split(" ", 3)[:3] for line in lines[:-1]] == [
        ["ok", "GSW", "lineup"],
        ["ok", "GSW", "on_court"],
        ["ok", "GSW", "on_court"],
    ]


def test_reconcile_command_names_the_missing_response(
    stints: sessionmaker[Session], reconcile_env: RawCache
) -> None:
    result = runner.invoke(app, ["reconcile", "--season", SEASON, "--teams", "GSW"])

    assert result.exit_code == 1
    assert result.output.splitlines() == [
        "reconcile: leaguedashlineups 2017-18_regular-season is not in the raw cache; "
        "run it with --refresh, or fetch --aggregates"
    ]


def test_reconcile_command_refuses_an_unknown_tricode() -> None:
    result = runner.invoke(app, ["reconcile", "--teams", "GSW,XYZ"])

    assert result.exit_code == 2
    assert "no team with tricode 'XYZ'" in result.output
