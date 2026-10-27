from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, or_, update
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.api.app import create_app
from closinglineup.db.models import (
    Game,
    ImpactSnapshot,
    InjuryStatus,
    Matchup,
    Player,
    PlayerFlag,
    PlayerGame,
    RosterMembership,
    RotationState,
    Shot,
    Stint,
    Transaction,
)
from closinglineup.derive.rotation import TierRule, compute_rotation_state
from closinglineup.derive.windows import refresh_windows
from closinglineup.settings import Settings
from closinglineup.sources.injury_pdf import EASTERN

# Seasons no other test seeds.
SEASON = "2032-33"
PRIOR_SEASON = "2031-32"
FIRST = date(2032, 11, 1)
PRIOR_DAY = date(2031, 11, 1)
PRIOR_END = date(2032, 4, 12)
GAMES = range(1, 13)
LAL = 1610612747
MIA = 1610612748
# He starts for MIA through game 6, is traded to LAL the day after, and starts there from game 7.
PLAYER = 9_400_001
LAL_FIVE = [9_400_002, 9_400_003, 9_400_004, 9_400_005, 9_400_006]
MIA_FIVE = [9_400_007, 9_400_008, 9_400_009, 9_400_010, 9_400_011]
PLAYERS = [PLAYER, *LAL_FIVE, *MIA_FIVE]
UNKNOWN = 9_400_099
TRADED = 6
TEAM_SECONDS = 14_400


def _game_id(g: int) -> str:
    return f"00232{g:05d}"


PRIOR_GAME = "0023100001"


def _day(g: int) -> date:
    return FIRST + timedelta(days=2 * (g - 1))


LAST = _day(GAMES[-1])
TRADE_DAY = _day(TRADED) + timedelta(days=1)
INJURY_DAY = _day(10)


def _with(g: int) -> int:
    return MIA if g <= TRADED else LAL


def _key(players: list[int]) -> str:
    return "-".join(str(p) for p in sorted(players))


def _fives(g: int) -> dict[int, tuple[list[int], list[int]]]:
    """Each team's five with him on the floor and without him: his team swaps its fifth man for
    him, the other plays its five throughout."""
    if _with(g) == MIA:
        return {
            MIA: ([PLAYER, *MIA_FIVE[:4]], MIA_FIVE),
            LAL: (LAL_FIVE, LAL_FIVE),
        }
    return {
        LAL: ([PLAYER, *LAL_FIVE[:4]], LAL_FIVE),
        MIA: (MIA_FIVE, MIA_FIVE),
    }


def _line(game_id: str, player_id: int, team_id: int, seconds: int) -> dict[str, Any]:
    counts = ("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl")
    return {
        "game_id": game_id,
        "player_id": player_id,
        "team_id": team_id,
        "is_starter": seconds >= 2400,
        "seconds": seconds,
        "dnp_reason": None,
        **dict.fromkeys((*counts, "blk", "tov", "pf", "plus_minus"), 0),
        "ts_pct": 0.6 if player_id == PLAYER else None,
        "usg_pct": 0.25 if player_id == PLAYER else None,
        "source": "stats",
    }


def _lines(game_id: str, g: int) -> list[dict[str, Any]]:
    """He plays 2400 seconds and his team's fifth man the other 480; everyone else 2880."""
    lines: list[dict[str, Any]] = []
    for team_id, (on, off) in _fives(g).items():
        bench = set(off) - set(on)
        for player_id in sorted({*on, *off}):
            seconds = 2400 if player_id == PLAYER else 480 if player_id in bench else 2880
            lines.append(_line(game_id, player_id, team_id, seconds))
    return lines


def _stints(game_id: str, g: int, garbage: bool = False) -> list[dict[str, Any]]:
    """His team wins the first quarter 26-20 on 25 a side with him, loses the second 10-15 on 10
    without him (garbage time if `garbage`), and closes 10-5 on 5 with him, one point up."""
    fives = _fives(g)
    mine = _with(g)
    theirs = LAL if mine == MIA else MIA
    rows: list[dict[str, Any]] = []
    for period, start, end, poss, scored, allowed, with_him, in_garbage in (
        (1, 0.0, 720.0, 25, 26, 20, True, False),
        (2, 0.0, 720.0, 10, 10, 15, False, garbage),
        (4, 600.0, 720.0, 5, 10, 5, True, False),
    ):
        mine_five = fives[mine][0 if with_him else 1]
        their_five = fives[theirs][0]
        for team_id, key, opp_key, pts_for, pts_against in (
            (mine, _key(mine_five), _key(their_five), scored, allowed),
            (theirs, _key(their_five), _key(mine_five), allowed, scored),
        ):
            rows.append(
                {
                    "game_id": game_id,
                    "period": period,
                    "start_sec": start,
                    "end_sec": end,
                    "team_id": team_id,
                    "lineup_key": key,
                    "opp_lineup_key": opp_key,
                    "possessions": poss,
                    "pts_for": pts_for,
                    "pts_against": pts_against,
                    "is_garbage_time": in_garbage,
                }
            )
    return rows


def _shots(game_id: str, team_id: int, zones: list[tuple[str, bool]]) -> list[dict[str, Any]]:
    return [
        {
            "game_id": game_id,
            "event_num": n,
            "player_id": PLAYER,
            "team_id": team_id,
            "period": 1,
            "clock_sec": 600,
            "x": 0,
            "y": 0,
            "distance_ft": 0,
            "zone": zone,
            "shot_type": "Jump Shot",
            "made": made,
            "assisted_by": None,
            "shot_clock_bucket": None,
        }
        for n, (zone, made) in enumerate(zones, start=1)
    ]


# Rim 2 of 3, corner three 0 of 1, above-the-break three 1 of 2.
SEASON_SHOTS = [
    ("rim", True),
    ("rim", True),
    ("rim", False),
    ("corner_3", False),
    ("above_break_3", True),
    ("above_break_3", False),
]


def _game(game_id: str, season: str, day: date) -> dict[str, Any]:
    return {
        "game_id": game_id,
        "season": season,
        "game_date": day,
        "phase": "regular",
        "home_team_id": LAL,
        "away_team_id": MIA,
        "status": "final",
        "home_score": 100,
        "away_score": 95,
        "home_b2b": False,
        "away_b2b": False,
    }


def _membership(player_id: int, team_id: int, start: date, end: date | None) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "team_id": team_id,
        "start_date": start,
        "end_date": end,
        "contract_type": "standard",
        "source": "player_game",
    }


def _injury(player_id: int, team_id: int, day: date, status: str, reason: str) -> dict[str, Any]:
    return {
        "report_datetime": datetime.combine(day, time(17), EASTERN),
        "game_date": day,
        "player_id": player_id,
        "team_id": team_id,
        "status": status,
        "reason": reason,
        "source": "pdf",
    }


def _matchup(g: int, off: int, defender: int, poss: float, pts: int) -> dict[str, Any]:
    return {
        "game_id": _game_id(g),
        "off_player_id": off,
        "def_player_id": defender,
        "partial_poss": poss,
        "pts": pts,
        "fgm": pts // 2,
        "fga": pts,
        "fg3m": 0,
        "fg3a": 0,
        "tov": 0,
        "ast": 0,
    }


def _flag(team_id: int, day: date, flag: str) -> dict[str, Any]:
    return {"player_id": PLAYER, "team_id": team_id, "game_date": day, "flag": flag, "detail": {}}


def _value(
    day: date, source: str, value: float, o_value: float | None = None, d_value: float | None = None
) -> dict[str, Any]:
    return {
        "date": day,
        "player_id": PLAYER,
        "source": source,
        "value": value,
        "o_value": o_value,
        "d_value": d_value,
    }


def clear(db: sessionmaker[Session]) -> None:
    game_ids = [PRIOR_GAME, *(_game_id(g) for g in GAMES)]
    with db.begin() as session:
        for model in (
            RotationState,
            PlayerFlag,
            InjuryStatus,
            Transaction,
            ImpactSnapshot,
            RosterMembership,
        ):
            session.execute(delete(model).where(model.player_id.in_(PLAYERS)))
        session.execute(delete(Player).where(Player.nba_player_id.in_(PLAYERS)))
        session.execute(
            delete(Matchup).where(
                or_(Matchup.off_player_id.in_(PLAYERS), Matchup.def_player_id.in_(PLAYERS))
            )
        )
        for model in (Shot, Stint, PlayerGame):
            session.execute(delete(model).where(model.game_id.in_(game_ids)))
        session.execute(delete(Game).where(Game.game_id.in_(game_ids)))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """LAL's 12 games against MIA with box scores, stints, his shot chart and matchups, and one in
    the prior season; his trade, an injury report listing him, impact and value snapshots and
    flags; the view refreshed and rotation state written for the last date."""
    clear(db)
    roster_from = PRIOR_DAY - timedelta(days=30)
    with db.begin() as session:
        session.execute(
            insert(Game),
            [
                _game(PRIOR_GAME, PRIOR_SEASON, PRIOR_DAY),
                *(_game(_game_id(g), SEASON, _day(g)) for g in GAMES),
            ],
        )
        session.execute(
            insert(PlayerGame),
            [
                *_lines(PRIOR_GAME, 1),
                *(line for g in GAMES for line in _lines(_game_id(g), g)),
            ],
        )
        session.execute(
            insert(Stint),
            [
                *_stints(PRIOR_GAME, 1),
                *(row for g in GAMES for row in _stints(_game_id(g), g, garbage=g == 11)),
            ],
        )
        session.execute(
            insert(Shot),
            [
                *_shots(PRIOR_GAME, MIA, [("rim", True), ("above_break_3", False)]),
                *(shot for g in GAMES for shot in _shots(_game_id(g), _with(g), SEASON_SHOTS)),
            ],
        )
        session.execute(
            insert(RosterMembership),
            [
                _membership(PLAYER, MIA, roster_from, TRADE_DAY - timedelta(days=1)),
                _membership(PLAYER, LAL, TRADE_DAY, None),
                *(_membership(p, LAL, roster_from, None) for p in LAL_FIVE),
                *(_membership(p, MIA, roster_from, None) for p in MIA_FIVE),
            ],
        )
        session.execute(
            insert(Player),
            [
                {
                    "nba_player_id": p,
                    "name": f"Player {p}",
                    "position": "G" if p == PLAYER else None,
                }
                for p in PLAYERS
            ],
        )
        session.execute(
            insert(Transaction),
            {
                "date": TRADE_DAY,
                "type": "trade",
                "player_id": PLAYER,
                "from_team_id": MIA,
                "to_team_id": LAL,
                "detail": "LAL acquired him from MIA",
            },
        )
        session.execute(
            insert(InjuryStatus),
            [
                _injury(PLAYER, LAL, INJURY_DAY, "Questionable", "Injury/Illness - Left Ankle"),
                # LAL's before he joined, MIA's after he left, and LAL's with him off it: none
                # changes his status.
                _injury(LAL_FIVE[4], LAL, _day(3), "Out", "Rest"),
                _injury(MIA_FIVE[4], MIA, _day(11), "Probable", "Rest"),
                _injury(LAL_FIVE[4], LAL, _day(8), "Out", "Rest"),
            ],
        )
        session.execute(
            insert(ImpactSnapshot),
            [
                {"date": FIRST, "player_id": PLAYER, "source": "bpm_local", "value": 1.0},
                {"date": INJURY_DAY, "player_id": PLAYER, "source": "bpm_local", "value": 2.0},
                {"date": _day(11), "player_id": PLAYER, "source": "darko", "value": 3.0},
                _value(_day(2), "ws_local", 0.3, 0.2, 0.1),
                _value(_day(7), "ws_local", 1.0, 0.6, 0.4),
                _value(_day(11), "ws_local", 1.5, 1.0, 0.5),
                _value(_day(12), "vorp_local", 0.3),
                _value(PRIOR_DAY, "ws_local", 0.2, 0.1, 0.1),
                _value(PRIOR_END, "ws_local", 4.0, 2.5, 1.5),
                _value(PRIOR_END, "vorp_local", 1.1),
            ],
        )
        session.execute(
            insert(Matchup),
            [
                _matchup(12, PLAYER, MIA_FIVE[0], 20.0, 8),
                _matchup(11, PLAYER, MIA_FIVE[0], 10.0, 4),
                _matchup(12, PLAYER, MIA_FIVE[1], 5.0, 2),
                _matchup(12, MIA_FIVE[2], PLAYER, 15.0, 6),
                # Before the 5-game window.
                _matchup(3, PLAYER, LAL_FIVE[3], 50.0, 20),
            ],
        )
        session.execute(
            insert(PlayerFlag),
            [
                _flag(LAL, _day(11), "earning_trust"),
                _flag(MIA, _day(2), "minutes_cliff"),
                _flag(MIA, PRIOR_DAY, "yo_yo"),
            ],
        )
    refresh_windows(db)
    compute_rotation_state(db, LAST, TierRule())
    yield db
    clear(db)
    refresh_windows(db)


@pytest.fixture
def client(database_url: str, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=database_url), tmp_path)) as client:
        yield client


def _page(client: TestClient, **params: str) -> dict[str, Any]:
    response = client.get(f"/api/players/{PLAYER}", params={"date": LAST.isoformat(), **params})
    assert response.status_code == 200, response.text
    return response.json()


def _dates(points: list[dict[str, Any]]) -> list[str]:
    return [point["game_date"] for point in points]


def test_the_annotations_are_his_injury_and_his_trade(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = _page(client, window="10")

    assert page["annotations"] == [
        {
            "game_date": TRADE_DAY.isoformat(),
            "kind": "transaction",
            "label": "LAL acquired him from MIA",
        },
        {
            "game_date": INJURY_DAY.isoformat(),
            "kind": "injury",
            "label": "Questionable: Injury/Illness - Left Ankle",
        },
    ]


def test_annotations_keep_to_the_window(seeded: sessionmaker[Session], client: TestClient) -> None:
    page = _page(client, window="5")

    assert [(note["game_date"], note["kind"]) for note in page["annotations"]] == [
        (INJURY_DAY.isoformat(), "injury")
    ]


def test_a_change_of_role_is_annotated(seeded: sessionmaker[Session], client: TestClient) -> None:
    with seeded.begin() as session:
        session.execute(
            update(PlayerGame)
            .where(PlayerGame.game_id == _game_id(9), PlayerGame.player_id == PLAYER)
            .values(is_starter=False)
        )
        # Off the floor at the close of game 10.
        session.execute(
            update(Stint)
            .where(Stint.game_id == _game_id(10), Stint.period == 4, Stint.team_id == LAL)
            .values(lineup_key=_key(LAL_FIVE))
        )

    notes = [(n["game_date"], n["kind"], n["label"]) for n in _page(client)["annotations"]]

    assert (_day(9).isoformat(), "starter", "Came off the bench") in notes
    assert (_day(10).isoformat(), "starter", "Started") in notes
    assert (_day(10).isoformat(), "closer", "Did not close") in notes
    assert (_day(11).isoformat(), "closer", "Closed") in notes


def _injury_notes(client: TestClient) -> list[tuple[str, str]]:
    page = _page(client, window="season")
    return [(n["game_date"], n["label"]) for n in page["annotations"] if n["kind"] == "injury"]


def test_his_old_team_s_report_after_a_trade_does_not_take_him_off_it(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    with seeded.begin() as session:
        session.execute(
            insert(InjuryStatus),
            [
                _injury(PLAYER, MIA, _day(5), "Out", "Rest"),
                # MIA's report the day of the trade, before his first LAL game, omits him.
                _injury(MIA_FIVE[4], MIA, TRADE_DAY, "Out", "Rest"),
            ],
        )

    assert _injury_notes(client) == [
        (_day(5).isoformat(), "Out: Rest"),
        (_day(8).isoformat(), "Off the injury report"),
        (INJURY_DAY.isoformat(), "Questionable: Injury/Illness - Left Ankle"),
    ]


def test_espn_s_fallback_is_read_only_where_no_official_report_covers_the_game(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    later = datetime.combine(INJURY_DAY, time(18), EASTERN)
    with seeded.begin() as session:
        session.execute(
            insert(InjuryStatus),
            {
                **_injury(LAL_FIVE[4], LAL, INJURY_DAY, "Out", "Rest"),
                "report_datetime": later,
                "source": "espn",
            },
        )

    assert _injury_notes(client) == [
        (INJURY_DAY.isoformat(), "Questionable: Injury/Illness - Left Ankle")
    ]


def test_window_5_is_his_last_5_team_game_dates(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = _page(client, window="5")

    expected = [_day(g).isoformat() for g in GAMES[-5:]]
    series = page["series"]
    for name in ("minutes", "minutes_share", "on_court_net", "on_off_net", "ts_pct", "usg_pct"):
        assert _dates(series[name]) == expected, name
    assert series["minutes"][-1]["value"] == pytest.approx(40.0)
    assert series["minutes_share"][-1]["value"] == pytest.approx(2400 / TEAM_SECONDS)
    # 36 for and 25 against on 30 a side with him; 10-15 on 10 without him.
    assert series["on_court_net"][-1]["value"] == pytest.approx(100 * 36 / 30 - 100 * 25 / 30)
    assert series["on_off_net"][-1]["value"] == pytest.approx(100 * 11 / 30 + 50)
    assert series["ts_pct"][-1]["value"] == pytest.approx(0.6)
    impact = {line["source"]: line["points"] for line in series["impact"]}
    assert list(impact) == ["darko", "bpm_local"]
    assert [p["value"] for p in impact["bpm_local"]] == [1.0, 1.0, 2.0, 2.0, 2.0]
    assert _dates(impact["darko"]) == expected[-2:]


def test_the_season_window_spans_both_teams(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = _page(client, window="season")

    assert _dates(page["series"]["minutes"]) == [_day(g).isoformat() for g in GAMES]


def test_garbage_time_is_left_out_unless_included(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    excluded = _page(client, window="5")
    included = _page(client, window="5", garbage_time="include")

    # Game 11's minutes without him are garbage time.
    assert excluded["garbage_time"] == "exclude"
    assert excluded["series"]["on_off_net"][-2]["value"] is None
    assert included["series"]["on_off_net"][-2]["value"] == pytest.approx(100 * 11 / 30 + 50)


def test_the_header_is_his_latest_rotation_state(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    header = _page(client)["header"]

    assert header == {
        "player": {"player_id": PLAYER, "name": f"Player {PLAYER}"},
        "position": "G",
        "tricode": "LAL",
        "tier": "starter",
        "badges": [],
    }


@pytest.mark.parametrize("window", ["5", "10", "20", "season"])
def test_shot_profile_frequencies_sum_to_one(
    seeded: sessionmaker[Session], client: TestClient, window: str
) -> None:
    profile = {zone["zone"]: zone for zone in _page(client, window=window)["shot_profile"]}

    assert list(profile) == ["rim", "short_mid", "long_mid", "corner_3", "above_break_3"]
    for column in ("freq", "season_freq", "prior_freq"):
        assert sum(zone[column] for zone in profile.values()) == pytest.approx(1.0, abs=0.001)
    assert profile["rim"]["freq"] == pytest.approx(0.5)
    assert profile["rim"]["fg_pct"] == pytest.approx(2 / 3)
    assert profile["above_break_3"]["season_freq"] == pytest.approx(1 / 3)
    assert profile["rim"]["prior_freq"] == pytest.approx(0.5)
    assert profile["short_mid"]["fg_pct"] is None


def test_before_his_first_game_the_shot_profile_is_the_prior_season_s(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = _page(client, date=(FIRST - timedelta(days=1)).isoformat())

    assert page["series"]["minutes"] == []
    assert page["header"]["tricode"] == "MIA"
    profile = {zone["zone"]: zone for zone in page["shot_profile"]}
    assert profile["rim"]["freq"] is None
    assert profile["rim"]["season_freq"] is None
    assert profile["rim"]["prior_freq"] == pytest.approx(0.5)
    assert profile["above_break_3"]["prior_fg_pct"] == pytest.approx(0.0)


PRODUCTION = [
    ("ts_pct", "TS%", "percent"),
    ("efg_pct", "eFG%", "percent"),
    ("usg_pct", "USG%", "percent"),
    ("ast_pct", "AST%", "percent"),
    ("tov_pct", "TOV%", "percent"),
    ("oreb_pct", "OREB%", "percent"),
    ("dreb_pct", "DREB%", "percent"),
    ("ftr", "FTr", "rate"),
    ("fg3a_rate", "3PAr", "rate"),
    ("pts_per36", "PTS/36", "number"),
    ("reb_per36", "REB/36", "number"),
    ("ast_per36", "AST/36", "number"),
    ("game_score", "Game Score", "per_game"),
    ("ws", "Win Shares", "number"),
    ("ws_48", "WS/48", "rate"),
    ("vorp", "VORP", "number"),
]


def _production(page: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["metric"]: row["windows"] for row in page["production"]}


def test_production_has_every_metric_in_order_with_its_format(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    page = _page(client)

    assert [(row["metric"], row["label"], row["format"]) for row in page["production"]] == (
        PRODUCTION
    )
    for windows in _production(page).values():
        assert list(windows) == ["5", "10", "20", "season", "prior_season"]


def test_production_windows_count_his_games_and_mark_a_short_one(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    pts = _production(_page(client))["pts_per36"]

    # 12 team games: the 20-game window is short, the 10-game one is not.
    assert (pts["10"]["games"], pts["10"]["is_short"]) == (10, False)
    assert (pts["20"]["games"], pts["20"]["is_short"]) == (12, True)
    assert (pts["season"]["games"], pts["season"]["is_short"]) == (12, False)
    assert pts["prior_season"]["games"] == 1
    assert pts["5"]["value"] == pytest.approx(0.0)
    assert pts["5"]["delta_season"] == pytest.approx(0.0)
    assert pts["5"]["o_value"] is None


def test_win_shares_are_over_each_window_with_no_delta_and_vorp_the_season_s_only(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    production = _production(_page(client))
    ws, vorp = production["ws"], production["vorp"]

    assert (vorp["5"], vorp["10"], vorp["20"]) == (None, None, None)
    for windows in (ws, vorp):
        for cell in windows.values():
            assert cell is None or cell["delta_season"] is None
    # His snapshot at game 12, carried from game 11, less those at games 7 and 2; the 20-game
    # window starts before his first game.
    assert (ws["5"]["value"], ws["5"]["o_value"], ws["5"]["d_value"]) == pytest.approx(
        (0.5, 0.4, 0.1)
    )
    assert (ws["10"]["value"], ws["10"]["o_value"]) == pytest.approx((1.2, 0.8))
    assert ws["20"]["value"] == 1.5
    season, prior = ws["season"], ws["prior_season"]
    assert (season["value"], season["o_value"], season["d_value"]) == (1.5, 1.0, 0.5)
    # The prior season's final snapshot, not its first.
    assert (prior["value"], prior["o_value"], prior["d_value"]) == (4.0, 2.5, 1.5)
    assert (vorp["season"]["value"], vorp["prior_season"]["value"]) == (0.3, 1.1)
    assert vorp["season"]["o_value"] is None


def test_ws_48_is_each_window_s_win_shares_per_48_minutes_with_its_delta(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    ws_48 = _production(_page(client))["ws_48"]

    # 40 minutes a game.
    assert ws_48["5"]["value"] == pytest.approx(0.5 * 48 / (5 * 40))
    assert ws_48["10"]["value"] == pytest.approx(1.2 * 48 / (10 * 40))
    assert ws_48["season"]["value"] == pytest.approx(1.5 * 48 / (12 * 40))
    assert ws_48["5"]["delta_season"] == pytest.approx(0.5 * 48 / 200 - 1.5 * 48 / 480)
    assert ws_48["season"]["delta_season"] == pytest.approx(0.0)
    assert ws_48["prior_season"]["value"] == pytest.approx(4.0 * 48 / 40)
    assert ws_48["5"]["o_value"] is None


def test_before_his_first_game_production_is_the_prior_season_s(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    production = _production(_page(client, date=(FIRST - timedelta(days=1)).isoformat()))

    for windows in production.values():
        assert [windows[name] for name in ("5", "10", "20", "season")] == [None] * 4
    pts = production["pts_per36"]["prior_season"]
    assert (pts["games"], pts["is_short"], pts["delta_season"]) == (1, False, None)
    ws = production["ws"]["prior_season"]
    assert (ws["value"], ws["o_value"], ws["d_value"]) == (4.0, 2.5, 1.5)
    assert production["ws_48"]["prior_season"]["delta_season"] is None
    assert production["vorp"]["prior_season"]["value"] == 1.1


def test_teammates_are_those_on_the_floor_with_him_most(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    teammates = _page(client, window="5")["teammates"]

    assert [mate["player"]["player_id"] for mate in teammates] == LAL_FIVE[:4]
    assert {mate["possessions"] for mate in teammates} == {5 * 30}
    assert teammates[0]["net_rating"] == pytest.approx(100 * 36 / 30 - 100 * 25 / 30)


def test_matchups_are_summed_over_the_window(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    matchups = _page(client, window="5")["matchups"]

    assert [
        (row["player"]["player_id"], row["partial_poss"], row["pts"])
        for row in matchups["defenders"]
    ] == [(MIA_FIVE[0], 30.0, 12), (MIA_FIVE[1], 5.0, 2)]
    assert [(row["player"]["player_id"], row["pts"]) for row in matchups["guarded"]] == [
        (MIA_FIVE[2], 6)
    ]


def test_flag_history_is_this_season_s_newest_first(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    flags = _page(client)["flags"]

    assert [(flag["game_date"], flag["tricode"], flag["flag"]) for flag in flags] == [
        (_day(11).isoformat(), "LAL", "earning_trust"),
        (_day(2).isoformat(), "MIA", "minutes_cliff"),
    ]


def test_an_unknown_player_is_404_with_a_json_body(
    seeded: sessionmaker[Session], client: TestClient
) -> None:
    response = client.get(f"/api/players/{UNKNOWN}", params={"date": LAST.isoformat()})

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"detail": f"No player {UNKNOWN}"}
