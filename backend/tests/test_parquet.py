import json
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from closinglineup.load.parquet import ROW_MODELS, ParquetSchemaError, ParquetStore, schema_for
from closinglineup.parse.box import parse_box
from closinglineup.parse.matchups import parse_matchups
from closinglineup.parse.roster import parse_players, parse_roster
from closinglineup.parse.rows import (
    GameRow,
    PossessionLineupRow,
    PossessionRow,
    Row,
    ShotRow,
    StintRow,
)
from closinglineup.parse.schedule import parse_schedule
from closinglineup.parse.shots import parse_shots

FIXTURES = Path(__file__).parent / "fixtures" / "nba_stats"
GAME = "0022500006"
DAY = date(2025, 10, 23)

type Payload = dict[str, Any]


def _fixture(endpoint: str, key: str = GAME) -> Payload:
    return json.loads((FIXTURES / endpoint / f"{key}.json").read_text())


def _rows() -> dict[str, list[Row]]:
    """Every table's rows, parsed from the fixtures."""
    roster = _fixture("commonteamroster", "1610612744_2025-26")
    return {
        "game": [*parse_schedule(_fixture("scheduleleaguev2", "2025-26"))],
        "player": [*parse_players(_fixture("commonallplayers", "2025-26"), [roster])],
        "roster_membership": [*parse_roster(roster, DAY)],
        "player_game": [
            *parse_box(
                _fixture("boxscoretraditionalv3"),
                _fixture("boxscoreadvancedv3"),
                _fixture("boxscorehustlev2"),
                _fixture("boxscoreusagev3"),
            )
        ],
        "matchup": [*parse_matchups(_fixture("boxscorematchupsv3"))],
        # pbpstats is exercised in test_pbp; these cover the columns, a NULL start type included.
        "stint": [
            StintRow(
                game_id=GAME,
                period=1,
                start_sec=0.0,
                end_sec=95.5,
                team_id=1610612744,
                lineup_key="1-2-3-4-5",
                opp_lineup_key="6-7-8-9-10",
                possessions=3,
                pts_for=5,
                pts_against=2,
            )
        ],
        "possession": [
            PossessionRow(
                game_id=GAME,
                period=1,
                number=number,
                offense_team_id=1610612744,
                start_sec=0.0,
                end_sec=14.0,
                start_score_margin=0,
                start_type=start_type,
                has_timeout=False,
                previous_has_timeout=False,
                end_type="FieldGoal",
                counted=True,
            )
            for number, start_type in [(1, "Off Deadball"), (2, None)]
        ],
        "possession_lineup": [
            PossessionLineupRow(
                game_id=GAME, period=1, number=1, team_id=1610612744, lineup_key=lineup_key
            )
            for lineup_key in ["1-2-3-4-5", "1-2-3-4-6"]
        ],
    }


@pytest.mark.parametrize("table", list(ROW_MODELS))
def test_a_tables_file_round_trips_its_rows_under_the_row_models_schema(
    tmp_path: Path, table: str
) -> None:
    rows = _rows()[table]
    store = ParquetStore(tmp_path)

    path = store.write_rows(table, DAY, rows)

    assert path == tmp_path / "parquet" / table / "2025-10-23.parquet"
    read = pq.read_table(path)  # pyright: ignore[reportUnknownMemberType]
    assert read.schema.equals(schema_for(ROW_MODELS[table]), check_metadata=False)
    assert read.column_names == list(ROW_MODELS[table].model_fields)
    model = ROW_MODELS[table]
    assert [model.model_validate(row) for row in store.read(table, DAY).to_pylist()] == rows


def test_the_schema_types_and_nullability_follow_the_row_model() -> None:
    assert schema_for(GameRow) == pa.schema(
        [
            pa.field("game_id", pa.string(), nullable=False),
            pa.field("season", pa.string(), nullable=False),
            pa.field("game_date", pa.date32(), nullable=False),
            pa.field("tip_time", pa.timestamp("us", tz="UTC"), nullable=True),
            pa.field("phase", pa.string(), nullable=False),
            pa.field("home_team_id", pa.int64(), nullable=False),
            pa.field("away_team_id", pa.int64(), nullable=False),
            pa.field("status", pa.string(), nullable=False),
            pa.field("home_score", pa.int64(), nullable=True),
            pa.field("away_score", pa.int64(), nullable=True),
            pa.field("home_b2b", pa.bool_(), nullable=False),
            pa.field("away_b2b", pa.bool_(), nullable=False),
        ]
    )


def test_rewriting_a_date_replaces_its_file_whole(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    rows = _rows()["matchup"]
    store.write_rows("matchup", DAY, rows)

    store.write_rows("matchup", DAY, rows[:3])

    assert store.read("matchup", DAY).num_rows == 3
    assert [p.name for p in (tmp_path / "parquet" / "matchup").iterdir()] == ["2025-10-23.parquet"]


def test_no_rows_still_writes_a_file_that_reads_as_empty(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)

    store.write_rows("player_game", DAY, [])

    assert store.read("player_game", DAY).num_rows == 0


def test_a_row_of_another_table_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="matchup takes MatchupRow, not GameRow"):
        ParquetStore(tmp_path).write_rows("matchup", DAY, _rows()["game"])


def test_a_file_written_from_another_schema_is_refused(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    path = store.path("game", DAY)
    path.parent.mkdir(parents=True)
    pq.write_table(pa.table({"game_id": [GAME]}), path)  # pyright: ignore[reportUnknownMemberType]

    with pytest.raises(ParquetSchemaError):
        store.read("game", DAY)


def test_a_date_never_written_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ParquetStore(tmp_path).read("game", DAY)


def test_dates_lists_every_date_with_a_file_for_any_table(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    store.write_rows("game", date(2025, 10, 24), [])
    store.write_rows("matchup", DAY, [])
    store.write_rows("game", DAY, [])
    store.write_rows("shot", SHOT_CHART, [])

    assert store.dates() == [DAY, date(2025, 10, 24)]


SHOT_CHART = "201939_2025-26_regular-season"


def test_a_shot_file_is_keyed_like_its_shot_chart_and_round_trips(tmp_path: Path) -> None:
    rows = parse_shots(_fixture("shotchartdetail", SHOT_CHART))
    store = ParquetStore(tmp_path)

    path = store.write_rows("shot", SHOT_CHART, rows)

    assert path == tmp_path / "parquet" / "shot" / f"{SHOT_CHART}.parquet"
    assert store.keys("shot") == [SHOT_CHART]
    assert [ShotRow.model_validate(r) for r in store.read("shot", SHOT_CHART).to_pylist()] == rows


def test_a_keyed_table_with_no_files_has_no_keys(tmp_path: Path) -> None:
    assert ParquetStore(tmp_path).keys("shot") == []
