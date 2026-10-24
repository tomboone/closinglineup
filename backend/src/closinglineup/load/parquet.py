"""Typed rows → one parquet file per table per game date, under `DATA_DIR/parquet` (spec §3.2).

The files are the contract between parsing and loading: `parse` rewrites a date's file for each
table whole, and `load` copies it into Postgres. `shot` is the exception: its rows come from one
shot chart per player, season and season type, so its files are keyed the way that shot chart is
cached (`<player_id>_<season>_<season type>`), and a weekly re-fetch rewrites one player's file.

Each file's schema is derived from its table's row model, so a file and the model it was written
from always agree, and `read` refuses a file written from an earlier model rather than loading it
under the wrong columns.
"""

from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from pathlib import Path
from types import NoneType, UnionType
from typing import Annotated, Any, Literal, TypeAliasType, Union, get_args, get_origin

import pyarrow as pa
import pyarrow.parquet as pq

from closinglineup.parse.rows import (
    GameRow,
    MatchupRow,
    PlayerGameRow,
    PlayerRow,
    PossessionLineupRow,
    PossessionRow,
    RosterMembershipRow,
    Row,
    ShotRow,
    StintRow,
)
from closinglineup.sources.cache import write_atomic

SUFFIX = ".parquet"

# Every table `parse` writes a file per game date for, by its row model, in an order that loads
# each table after the ones its foreign keys reference.
ROW_MODELS: dict[str, type[Row]] = {
    "game": GameRow,
    "player": PlayerRow,
    "roster_membership": RosterMembershipRow,
    "player_game": PlayerGameRow,
    "matchup": MatchupRow,
    "stint": StintRow,
    "possession": PossessionRow,
    "possession_lineup": PossessionLineupRow,
}
# Every table whose files are keyed by the raw cache key of the response they are parsed from.
KEYED_ROW_MODELS: dict[str, type[Row]] = {"shot": ShotRow}

# A file's key: its game date, or for a `KEYED_ROW_MODELS` table, a raw cache key.
type FileKey = date | str

_ARROW_TYPES: dict[Any, pa.DataType] = {
    bool: pa.bool_(),
    int: pa.int64(),
    float: pa.float64(),
    str: pa.string(),
    date: pa.date32(),
}


class ParquetSchemaError(ValueError):
    """A parquet file's schema is not its table's row model's."""


def _arrow_type(annotation: Any) -> tuple[pa.DataType, bool]:
    """The arrow type of a row field's annotation, and whether it is nullable: `X | None` is a
    nullable X, and a `Literal` of strings, a `StrEnum` or a constrained `str` is a string."""
    nullable = False
    while True:
        if isinstance(annotation, TypeAliasType):
            annotation = annotation.__value__
            continue
        origin = get_origin(annotation)
        if origin is Annotated:
            annotation = get_args(annotation)[0]
        elif origin in (Union, UnionType):
            nullable = True
            (annotation,) = (arg for arg in get_args(annotation) if arg is not NoneType)
        elif origin is Literal:
            if not all(isinstance(value, str) for value in get_args(annotation)):
                raise TypeError(f"no arrow type for {annotation}")
            return pa.string(), nullable
        else:
            break
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return pa.string(), nullable
    try:
        return _ARROW_TYPES[annotation], nullable
    except KeyError:
        raise TypeError(f"no arrow type for {annotation}") from None


def schema_for(model: type[Row]) -> pa.Schema:
    """The arrow schema of `model`'s rows: a column per field, in field order."""
    fields: list[pa.Field[Any]] = []
    for name, info in model.model_fields.items():
        arrow_type, nullable = _arrow_type(info.annotation)
        fields.append(pa.field(name, arrow_type, nullable=nullable))
    return pa.schema(fields)


def row_model(table: str) -> type[Row]:
    return ROW_MODELS.get(table) or KEYED_ROW_MODELS[table]


class ParquetStore:
    """`<data_dir>/parquet/<table>/<key>.parquet`: one file per table per game date, or per raw
    cache key for a `KEYED_ROW_MODELS` table."""

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "parquet"

    def path(self, table: str, key: FileKey) -> Path:
        name = key.isoformat() if isinstance(key, date) else key
        return self.root / table / f"{name}{SUFFIX}"

    def write_rows(self, table: str, key: FileKey, rows: Sequence[Row]) -> Path:
        """Write `rows` as the file for `table` and `key`, replacing any earlier one atomically;
        no rows still writes a file, which loads as nothing."""
        model = row_model(table)
        if strays := {type(row).__name__ for row in rows if type(row) is not model}:
            raise TypeError(f"{table} takes {model.__name__}, not {', '.join(sorted(strays))}")
        data = pa.Table.from_pylist([row.model_dump() for row in rows], schema=schema_for(model))
        sink = pa.BufferOutputStream()
        # pyarrow-stubs leaves a few of these two functions' parameter types unknown.
        pq.write_table(data, sink)  # pyright: ignore[reportUnknownMemberType]
        path = self.path(table, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, sink.getvalue().to_pybytes())
        return path

    def read(self, table: str, key: FileKey) -> pa.Table:
        """The file for `table` and `key`; raises `FileNotFoundError` if it was never written,
        and `ParquetSchemaError` if it was written from a different row model."""
        path = self.path(table, key)
        data = pq.read_table(path)  # pyright: ignore[reportUnknownMemberType]
        expected = schema_for(row_model(table))
        if not data.schema.equals(expected, check_metadata=False):
            raise ParquetSchemaError(f"{path} has schema {data.schema}, not {expected}")
        return data

    def dates(self) -> list[date]:
        """Every game date with a file for any table, sorted."""
        found = {
            date.fromisoformat(path.name.removesuffix(SUFFIX))
            for table in ROW_MODELS
            if (self.root / table).is_dir()
            for path in (self.root / table).iterdir()
            if path.name.endswith(SUFFIX)
        }
        return sorted(found)

    def keys(self, table: str) -> list[str]:
        """Every key a `KEYED_ROW_MODELS` table has a file for, sorted."""
        directory = self.root / table
        if not directory.is_dir():
            return []
        return sorted(
            path.name.removesuffix(SUFFIX)
            for path in directory.iterdir()
            if path.name.endswith(SUFFIX)
        )
