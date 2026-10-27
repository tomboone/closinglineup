from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.orm import Session, sessionmaker

from closinglineup.db.models import (
    Game,
    ImpactSnapshot,
    PlayerGame,
    RosterMembership,
    Shot,
    Stint,
)
from closinglineup.derive.windows import refresh_windows

DB_DIR = Path(__file__).parents[1] / "src" / "closinglineup" / "db"

SEASON = "2026-27"
PRIOR_SEASON = "2025-26"
# SAC's game g, 1 to 20, is on DAY + g - 1 against POR; UTA's games 7 to 12 fall on the same
# dates against ORL.
DAY = date(2026, 10, 21)
SAC = 1610612758
POR = 1610612757
UTA = 1610612762
ORL = 1610612753
SAC_GAMES = range(1, 21)
UTA_GAMES = range(7, 13)
OVERTIME = 5
PRIOR_GAME = "0022500990"
PRIOR_DAY = date(2026, 3, 1)
PRIOR_SEASON_END = date(2026, 4, 12)

# The fixture player, on SAC all season and for one game of the last: he plays 20 + g minutes in
# game g, starting the even ones, but sits game 3 by coach's decision and is missing from game 7's
# box altogether.
FIXTURE = 950001
DNP_CD = 3
ABSENT = 7
# Traded from SAC to UTA after SAC's sixth game: 20 minutes a game for SAC, 30 for UTA.
TRADED = 950002
TRADE_DAY = DAY + timedelta(days=6)
# Each team's other minutes, so its players' seconds sum to five times the game's length.
SAC_BENCH = 950003
POR_BENCH = 950011
UTA_BENCH = 950012
ORL_BENCH = 950013
PLAYERS = [FIXTURE, TRADED, SAC_BENCH, POR_BENCH, UTA_BENCH, ORL_BENCH]
# The rest of SAC's and POR's fives in the stints: players with no box line or roster span.
FIXTURE_ON = f"{FIXTURE}-{SAC_BENCH}-950004-950005-950006"
FIXTURE_OFF = f"{SAC_BENCH}-950004-950005-950006-950007"
POR_FIVE = f"{POR_BENCH}-950014-950015-950016-950017"


def _day(g: int) -> date:
    return DAY + timedelta(days=g - 1)


def _sac_game(g: int) -> str:
    return f"00226009{g:02d}"


def _uta_game(g: int) -> str:
    return f"00226009{g + 43:02d}"


def _game_seconds(g: int) -> int:
    """Five players over SAC's game g, the overtime one included."""
    return 5 * (2880 + 300 * (g == OVERTIME))


def _fixture_minutes(g: int) -> int:
    return 0 if g in (DNP_CD, ABSENT) else 20 + g


def _line(
    game_id: str,
    player_id: int,
    team_id: int,
    seconds: int,
    *,
    starter: bool = False,
    dnp_reason: str | None = None,
    pts: int = 0,
    fgm: int = 0,
    fga: int = 0,
    ftm: int = 0,
    fta: int = 0,
    oreb: int = 0,
    dreb: int = 0,
    ast: int = 0,
    stl: int = 0,
    blk: int = 0,
    tov: int = 0,
    pf: int = 0,
) -> PlayerGame:
    return PlayerGame(
        game_id=game_id,
        player_id=player_id,
        team_id=team_id,
        is_starter=starter,
        seconds=seconds,
        dnp_reason=dnp_reason,
        pts=pts,
        fgm=fgm,
        fga=fga,
        fg3m=0,
        fg3a=0,
        ftm=ftm,
        fta=fta,
        oreb=oreb,
        dreb=dreb,
        ast=ast,
        stl=stl,
        blk=blk,
        tov=tov,
        pf=pf,
        plus_minus=0,
        source="stats",
    )


def _game(game_id: str, season: str, day: date, home: int, away: int) -> Game:
    return Game(
        game_id=game_id,
        season=season,
        game_date=day,
        phase="regular",
        home_team_id=home,
        away_team_id=away,
        status="final",
        home_b2b=False,
        away_b2b=False,
    )


def _sac_lines(g: int) -> list[PlayerGame]:
    """SAC's and POR's box in game g. The fixture player scores g on 4 of 10 and 3 of 4 free
    throws, with 1 offensive and 4 defensive rebounds, 5 assists, 2 steals, a block, 3 turnovers
    and 2 fouls; the traded player 8 on 3 of 6 with 2 free throw attempts."""
    game_id = _sac_game(g)
    total = _game_seconds(g)
    fixture = _fixture_minutes(g) * 60
    traded = 20 * 60 if g < 7 else 0
    lines = [
        _line(game_id, SAC_BENCH, SAC, total - fixture - traded, oreb=10, dreb=30),
        _line(game_id, POR_BENCH, POR, total, oreb=10, dreb=30),
    ]
    if g == DNP_CD:
        lines.append(_line(game_id, FIXTURE, SAC, 0, dnp_reason="cd"))
    elif g != ABSENT:
        lines.append(
            _line(
                game_id,
                FIXTURE,
                SAC,
                fixture,
                starter=g % 2 == 0,
                pts=g,
                fgm=4,
                fga=10,
                ftm=3,
                fta=4,
                oreb=1,
                dreb=4,
                ast=5,
                stl=2,
                blk=1,
                tov=3,
                pf=2,
            )
        )
    if g < 7:
        lines.append(_line(game_id, TRADED, SAC, traded, pts=8, fgm=3, fga=6, fta=2))
    return lines


def _stints(g: int) -> list[Stint]:
    """SAC's and POR's views of game g's stints. With the fixture player on the floor if he
    played: the first quarter, 20 possessions a side scored 24-20, and a garbage-time stretch of
    the fourth, 10 a side at 10-10; then the fourth's last 320 seconds, 8 a side at 10-6, which
    starts tied and so is a closing stint, with him on the floor in the odd games. Without him:
    the second quarter, 20 a side at 18-22."""
    played = _fixture_minutes(g) > 0
    rows: list[Stint] = []
    for period, start, end, on, poss, pts_for, pts_against, garbage in (
        (1, 0.0, 720.0, played, 20, 24, 20, False),
        (2, 0.0, 720.0, False, 20, 18, 22, False),
        (4, 0.0, 400.0, played, 10, 10, 10, True),
        (4, 400.0, 720.0, played and g % 2 == 1, 8, 10, 6, False),
    ):
        five = FIXTURE_ON if on else FIXTURE_OFF
        for team_id, key, opp_key, scored, allowed in (
            (SAC, five, POR_FIVE, pts_for, pts_against),
            (POR, POR_FIVE, five, pts_against, pts_for),
        ):
            rows.append(
                Stint(
                    game_id=_sac_game(g),
                    period=period,
                    start_sec=start,
                    end_sec=end,
                    team_id=team_id,
                    lineup_key=key,
                    opp_lineup_key=opp_key,
                    possessions=poss,
                    pts_for=scored,
                    pts_against=allowed,
                    is_garbage_time=garbage,
                )
            )
    return rows


def _shot(event_num: int, zone: str, made: bool) -> Shot:
    return Shot(
        game_id=_sac_game(20),
        event_num=event_num,
        player_id=FIXTURE,
        team_id=SAC,
        period=1,
        clock_sec=600,
        x=0,
        y=0,
        distance_ft=2,
        zone=zone,
        shot_type="Jump Shot",
        made=made,
    )


def _membership(player_id: int, team_id: int, start: date, end: date | None) -> RosterMembership:
    return RosterMembership(
        player_id=player_id,
        team_id=team_id,
        start_date=start,
        end_date=end,
        contract_type="standard",
        source="player_game",
    )


def _snapshot(day: date, source: str, value: float, o_value: float | None) -> ImpactSnapshot:
    return ImpactSnapshot(
        date=day,
        player_id=FIXTURE,
        source=source,
        value=value,
        o_value=o_value,
        d_value=None if o_value is None else value - o_value,
        minutes_basis=None,
    )


GAME_IDS = [
    *(_sac_game(g) for g in SAC_GAMES),
    *(_uta_game(g) for g in UTA_GAMES),
    PRIOR_GAME,
]


def _clear(db: sessionmaker[Session]) -> None:
    with db.begin() as session:
        session.execute(delete(Shot).where(Shot.game_id.in_(GAME_IDS)))
        session.execute(delete(Stint).where(Stint.game_id.in_(GAME_IDS)))
        session.execute(delete(PlayerGame).where(PlayerGame.game_id.in_(GAME_IDS)))
        session.execute(delete(ImpactSnapshot).where(ImpactSnapshot.player_id.in_(PLAYERS)))
        session.execute(delete(RosterMembership).where(RosterMembership.player_id.in_(PLAYERS)))
        session.execute(delete(Game).where(Game.game_id.in_(GAME_IDS)))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    """The games, stints, shots, roster spans and impact snapshots above, and the view refreshed
    over them. The fixture player also played SAC's one game of 2025-26, scoring 20 on 10 shots
    and 5 free throw attempts in 30 minutes; and has a BPM on SAC's games 18 and 20 and an EPM
    on its first, Win Shares on its game 18 and VORP on its 19, and Win Shares twice and VORP
    once in 2025-26."""
    _clear(db)
    with db.begin() as session:
        session.add_all(_game(_sac_game(g), SEASON, _day(g), SAC, POR) for g in SAC_GAMES)
        session.add_all(_game(_uta_game(g), SEASON, _day(g), UTA, ORL) for g in UTA_GAMES)
        session.add(_game(PRIOR_GAME, PRIOR_SEASON, PRIOR_DAY, SAC, POR))
        session.flush()
        for g in SAC_GAMES:
            session.add_all(_sac_lines(g))
            session.add_all(_stints(g))
        for g in UTA_GAMES:
            session.add_all(
                [
                    _line(_uta_game(g), TRADED, UTA, 30 * 60, pts=12, fgm=5, fga=8, fta=4),
                    _line(_uta_game(g), UTA_BENCH, UTA, 14400 - 30 * 60),
                    _line(_uta_game(g), ORL_BENCH, ORL, 14400),
                ]
            )
        session.add_all(
            [
                _line(PRIOR_GAME, FIXTURE, SAC, 30 * 60, pts=20, fgm=8, fga=10, fta=5),
                _line(PRIOR_GAME, SAC_BENCH, SAC, 14400 - 30 * 60),
                _line(PRIOR_GAME, POR_BENCH, POR, 14400),
            ]
        )
        session.add_all(
            [
                _shot(1, "rim", True),
                _shot(2, "rim", False),
                _shot(3, "corner_3", True),
                _shot(4, "corner_3", True),
            ]
        )
        session.add_all(
            [
                _membership(FIXTURE, SAC, date(2026, 2, 1), date(2026, 4, 15)),
                _membership(FIXTURE, SAC, DAY, None),
                _membership(TRADED, SAC, DAY, TRADE_DAY - timedelta(days=1)),
                _membership(TRADED, UTA, TRADE_DAY, None),
                _membership(SAC_BENCH, SAC, date(2026, 2, 1), None),
                _membership(POR_BENCH, POR, date(2026, 2, 1), None),
                _membership(UTA_BENCH, UTA, DAY, None),
                _membership(ORL_BENCH, ORL, DAY, None),
                _snapshot(_day(1), "epm", 1.0, 0.5),
                _snapshot(_day(18), "bpm_local", 2.5, 1.5),
                _snapshot(_day(20), "bpm_local", 3.0, 2.0),
                _snapshot(_day(18), "ws_local", 1.2, 0.8),
                _snapshot(_day(19), "vorp_local", 0.4, None),
                _snapshot(PRIOR_DAY, "ws_local", 0.5, 0.3),
                _snapshot(PRIOR_SEASON_END, "ws_local", 0.9, 0.6),
                _snapshot(PRIOR_SEASON_END, "vorp_local", 0.2, None),
            ]
        )
    refresh_windows(db)
    yield db
    _clear(db)
    refresh_windows(db)


def _rows(
    db: sessionmaker[Session], player_id: int, team_id: int | None, window: str
) -> dict[date, dict[str, Any]]:
    """The player's rows for one team (None, his production rows) and window, by date."""
    with db() as session:
        rows = session.execute(
            text(
                'SELECT * FROM player_window_stats WHERE player_id = :player_id AND "window" = '
                ":window AND team_id IS NOT DISTINCT FROM :team_id ORDER BY game_date"
            ),
            {"player_id": player_id, "team_id": team_id, "window": window},
        ).mappings()
        return {row["game_date"]: dict(row) for row in rows}


def _row(
    db: sessionmaker[Session], player_id: int, team_id: int | None, window: str, day: date
) -> dict[str, Any]:
    return _rows(db, player_id, team_id, window)[day]


def test_the_fixture_players_5_game_role_window_matches_the_hand_computed_values(
    seeded: sessionmaker[Session],
) -> None:
    last = _row(seeded, FIXTURE, SAC, "5", _day(20))

    # Games 16 to 20: 36 to 40 minutes, starting 16, 18 and 20, in five regulation games.
    assert last["games_in_window"] == 5
    assert last["is_short"] is False
    assert last["minutes_share"] == pytest.approx((36 + 37 + 38 + 39 + 40) * 60 / (5 * 14400))
    assert last["minutes_per_game"] == pytest.approx(38)
    assert last["minutes_volatility"] == pytest.approx(2.5**0.5)
    assert last["starter_rate"] == pytest.approx(3 / 5)
    # Each game has a closing stint; he is on the floor for those of 17 and 19.
    assert last["closer_rate"] == pytest.approx(2 / 5)
    # The bench player has the rest of the minutes.
    assert last["rotation_rank"] == 2
    # On the floor, garbage time excluded: five first quarters (20 possessions a side, 24-20) and
    # the closing stints of 17 and 19 (8 a side, 10-6). Off it: the five second quarters and the
    # other three closing stints.
    assert last["on_court_poss"] == 5 * 20 + 2 * 8
    assert last["on_court_opp_poss"] == 5 * 20 + 2 * 8
    assert last["off_court_poss"] == 5 * 20 + 3 * 8
    on_net = 100 * (5 * 24 + 2 * 10) / 116 - 100 * (5 * 20 + 2 * 6) / 116
    off_net = 100 * (5 * 18 + 3 * 10) / 124 - 100 * (5 * 22 + 3 * 6) / 124
    assert last["on_court_net"] == pytest.approx(on_net)
    assert last["on_off_net"] == pytest.approx(on_net - off_net)


def test_the_5_game_window_is_short_in_the_first_4_team_games_and_counts_overtime(
    seeded: sessionmaker[Session],
) -> None:
    rows = _rows(seeded, FIXTURE, SAC, "5")

    assert [rows[_day(g)]["is_short"] for g in range(1, 7)] == [True] * 4 + [False] * 2
    assert [rows[_day(g)]["games_in_window"] for g in range(1, 7)] == [1, 2, 3, 4, 5, 5]
    # Games 1 to 5: 21, 22, 24 and 25 minutes, the coach's decision in 3, and an overtime in 5.
    assert rows[_day(5)]["minutes_share"] == pytest.approx(
        (21 + 22 + 24 + 25) * 60 / (4 * 14400 + 15900)
    )
    assert rows[_day(5)]["dnp_cd_count"] == 1
    assert rows[_day(5)]["games_played"] == 4


def test_the_10_game_deltas_compare_games_11_to_20_with_1_to_10_and_the_season(
    seeded: sessionmaker[Session],
) -> None:
    last = _row(seeded, FIXTURE, SAC, "10", _day(20))
    earlier = _row(seeded, FIXTURE, SAC, "10", _day(10))
    season = _row(seeded, FIXTURE, SAC, "season", _day(20))

    share_11_to_20 = sum(range(31, 41)) * 60 / (10 * 14400)
    share_1_to_10 = sum(_fixture_minutes(g) for g in range(1, 11)) * 60 / (9 * 14400 + 15900)
    assert last["minutes_share"] == pytest.approx(share_11_to_20)
    assert earlier["minutes_share"] == pytest.approx(share_1_to_10)
    assert last["minutes_share_delta_prior"] == pytest.approx(share_11_to_20 - share_1_to_10)
    assert last["minutes_share_delta_season"] == pytest.approx(
        share_11_to_20 - season["minutes_share"]
    )
    assert season["games_in_window"] == 20
    assert season["is_short"] is False
    # Games 1 to 10 hold the coach's decision and the game he is missing from the box.
    assert (earlier["dnp_cd_count"], earlier["inactive_count"]) == (1, 1)
    assert last["dnp_cd_count_delta_prior"] == -1
    # A window has no prior until the player's (n + 1)th team game.
    assert earlier["minutes_share_delta_prior"] is None


def test_the_fixture_players_production_windows(seeded: sessionmaker[Session]) -> None:
    last = _row(seeded, FIXTURE, None, "5", _day(20))
    season = _row(seeded, FIXTURE, None, "season", _day(20))
    prior = _row(seeded, FIXTURE, None, "prior_season", _day(20))

    # Games 16 to 20: 16 to 20 points on 10 field goal attempts and 4 free throw attempts each.
    ts_pct = (16 + 17 + 18 + 19 + 20) / (2 * (50 + 0.44 * 20))
    assert last["team_id"] is None
    assert last["ts_pct"] == pytest.approx(ts_pct)
    assert last["efg_pct"] == pytest.approx(20 / 50)
    assert last["pts_per36"] == pytest.approx(90 * 36 / (36 + 37 + 38 + 39 + 40))
    # Per 100 possessions counts garbage time, as the box score does: 10 more a game.
    assert last["pts_per100"] == pytest.approx(90 * 100 / (5 * 20 + 2 * 8 + 5 * 10))
    assert last["rim_freq"] == pytest.approx(0.5)
    assert last["rim_fg_pct"] == pytest.approx(0.5)
    assert last["corner_3_fg_pct"] == pytest.approx(1.0)
    assert last["long_mid_fg_pct"] is None
    # The season, 18 games played, against the one game of the season before.
    season_ts = (sum(SAC_GAMES) - DNP_CD - ABSENT) / (2 * (180 + 0.44 * 72))
    prior_ts = 20 / (2 * (10 + 0.44 * 5))
    assert season["ts_pct"] == pytest.approx(season_ts)
    assert prior["ts_pct"] == pytest.approx(prior_ts)
    assert prior["games_in_window"] == 1
    assert season["ts_pct_delta_prior"] == pytest.approx(season_ts - prior_ts)
    assert last["ts_pct_delta_season"] == pytest.approx(ts_pct - season_ts)
    # The traded player played no game of the season before.
    assert _rows(seeded, TRADED, None, "prior_season") == {}


def test_the_fixture_players_game_score_is_the_formula_over_the_window_per_game_played(
    seeded: sessionmaker[Session],
) -> None:
    rows = _rows(seeded, FIXTURE, None, "5")
    season = _row(seeded, FIXTURE, None, "season", _day(20))
    prior = _row(seeded, FIXTURE, None, "prior_season", _day(20))

    # Game g's line: g + 0.4·4 - 0.7·10 - 0.4·(4 - 3) + 0.7·1 + 0.3·4 + 2 + 0.7·5 + 0.7·1 - 0.4·2
    # - 3, which is g - 1.5. Games 16 to 20 average 16.5, games 11 to 15 11.5.
    last = rows[_day(20)]
    assert last["game_score"] == pytest.approx(16.5, abs=1e-6)
    assert last["game_score_delta_prior"] == pytest.approx(16.5 - 11.5, abs=1e-6)
    # The season: 18 games played, all but the coach's decision in 3 and the absence in 7.
    season_score = (sum(SAC_GAMES) - DNP_CD - ABSENT) / 18 - 1.5
    assert season["game_score"] == pytest.approx(season_score, abs=1e-6)
    assert last["game_score_delta_season"] == pytest.approx(16.5 - season_score, abs=1e-6)
    # Divided by games played, not the window's games: games 1 to 5 hold the coach's decision.
    assert rows[_day(5)]["game_score"] == pytest.approx((1 + 2 + 4 + 5) / 4 - 1.5, abs=1e-6)
    # The season before: 20 + 0.4·8 - 0.7·10 - 0.4·5.
    assert prior["game_score"] == pytest.approx(14.2, abs=1e-6)
    assert season["game_score_delta_prior"] == pytest.approx(season_score - 14.2, abs=1e-6)


def test_game_score_is_null_over_a_window_he_played_none_of(
    seeded: sessionmaker[Session],
) -> None:
    with seeded.begin() as session:
        session.execute(
            update(PlayerGame)
            .where(
                PlayerGame.player_id == FIXTURE,
                PlayerGame.game_id.in_([_sac_game(g) for g in range(16, 21)]),
            )
            .values(seconds=0, dnp_reason="cd")
        )

    refresh_windows(seeded)

    last = _row(seeded, FIXTURE, None, "5", _day(20))
    assert last["games_in_window"] == 5
    assert last["game_score"] is None
    assert last["game_score_delta_prior"] is None


def test_the_latest_impact_snapshot_of_each_source_is_carried_forward(
    seeded: sessionmaker[Session],
) -> None:
    rows = _rows(seeded, FIXTURE, None, "10")

    assert (rows[_day(17)]["epm"], rows[_day(17)]["bpm_local"]) == (1.0, None)
    assert (rows[_day(19)]["bpm_local"], rows[_day(19)]["bpm_local_o"]) == (2.5, 1.5)
    assert (rows[_day(20)]["bpm_local"], rows[_day(20)]["bpm_local_d"]) == (3.0, 1.0)
    assert rows[_day(20)]["epm"] == 1.0
    assert rows[_day(20)]["darko"] is None


def test_the_value_snapshots_carry_forward_within_the_season_and_prior_reads_its_final(
    seeded: sessionmaker[Session],
) -> None:
    rows = _rows(seeded, FIXTURE, None, "season")
    prior = _row(seeded, FIXTURE, None, "prior_season", _day(20))

    # 2025-26's snapshots do not carry into 2026-27, whose first comes on game 18.
    assert (rows[_day(17)]["ws_local"], rows[_day(17)]["vorp_local"]) == (None, None)
    assert (rows[_day(18)]["ws_local"], rows[_day(18)]["vorp_local"]) == (1.2, None)
    assert rows[_day(20)]["ws_local"] == 1.2
    assert rows[_day(20)]["ws_local_o"] == 0.8
    assert rows[_day(20)]["ws_local_d"] == pytest.approx(0.4)
    assert rows[_day(20)]["vorp_local"] == 0.4
    assert _row(seeded, FIXTURE, None, "5", _day(20))["ws_local"] == 1.2
    assert (prior["ws_local"], prior["ws_local_o"], prior["vorp_local"]) == (0.9, 0.6, 0.2)
    assert prior["ws_local_d"] == pytest.approx(0.3)


def test_a_traded_players_role_windows_restart_and_his_production_windows_continue(
    seeded: sessionmaker[Session],
) -> None:
    sac = _rows(seeded, TRADED, SAC, "5")
    uta = _rows(seeded, TRADED, UTA, "5")
    production = _rows(seeded, TRADED, None, "5")

    assert list(sac) == [_day(g) for g in range(1, 7)]
    assert list(uta) == [_day(g) for g in UTA_GAMES]
    assert [row["games_in_window"] for row in uta.values()] == [1, 2, 3, 4, 5, 5]
    assert uta[TRADE_DAY]["is_short"] is True
    assert uta[TRADE_DAY]["minutes_share"] == pytest.approx(30 * 60 / 14400)
    assert uta[TRADE_DAY]["rotation_rank"] == 2
    assert list(production) == [_day(g) for g in range(1, 13)]
    first_for_uta = production[TRADE_DAY]
    assert first_for_uta["games_in_window"] == 5
    assert first_for_uta["is_short"] is False
    # SAC's games 3 to 6 and UTA's first.
    assert first_for_uta["ts_pct"] == pytest.approx((4 * 8 + 12) / (2 * (4 * 6 + 8 + 0.44 * 12)))


def test_refresh_windows_is_idempotent(seeded: sessionmaker[Session]) -> None:
    before = _rows(seeded, FIXTURE, SAC, "20")

    refreshed = refresh_windows(seeded)

    assert set(refreshed) == {"rows", "seconds"}
    assert refreshed["rows"] > 0
    assert _rows(seeded, FIXTURE, SAC, "20") == before


def test_a_second_game_on_a_teams_date_counts_once_and_the_refresh_holds(
    seeded: sessionmaker[Session],
) -> None:
    before = _row(seeded, FIXTURE, SAC, "5", _day(20))
    with seeded.begin() as session:
        session.add(_game("0022600999", SEASON, _day(20), POR, SAC))

    refresh_windows(seeded)

    assert _row(seeded, FIXTURE, SAC, "5", _day(20)) == before
    with seeded.begin() as session:
        session.execute(delete(Game).where(Game.game_id == "0022600999"))


def test_views_sql_is_the_view_migration_s_copy() -> None:
    copy = DB_DIR / "alembic" / "versions" / "c7e19a4d2b30_player_window_stats_value_snapshots.sql"

    assert (DB_DIR / "views.sql").read_text() == copy.read_text()
