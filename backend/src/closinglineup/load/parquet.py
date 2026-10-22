"""Typed rows → one parquet file per table per game date, under `DATA_DIR/parquet` (spec §3.2).

The files are the contract between parsing and loading: `parse` rewrites a date's file for each
table whole, and `load` copies it into Postgres. Each file's schema is derived from its table's
row model, so a file and the model it was written from always agree, and `read` refuses a file
written from an earlier model rather than loading it under the wrong columns.
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
    RosterMembershipRow,
    Row,
)
from closinglineup.sources.cache import write_atomic

SUFFIX = ".parquet"

# Every table `parse` writes, by its row model, in an order that loads each table after the ones
# its foreign keys reference.
ROW_MODELS: dict[str, type[Row]] = {
    "game": GameRow,
    "player": PlayerRow,
    "roster_membership": RosterMembershipRow,
    "player_game": PlayerGameRow,
    "matchup": MatchupRow,
}

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


class ParquetStore:
    """`<data_dir>/parquet/<table>/<game_date>.parquet`, one file per table per game date."""

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "parquet"

    def path(self, table: str, game_date: date) -> Path:
        return self.root / table / f"{game_date.isoformat()}{SUFFIX}"

    def write_rows(self, table: str, game_date: date, rows: Sequence[Row]) -> Path:
        """Write `rows` as the date's file for `table`, replacing any earlier one atomically; no
        rows still writes a file, which loads as nothing."""
        model = ROW_MODELS[table]
        if strays := {type(row).__name__ for row in rows if type(row) is not model}:
            raise TypeError(f"{table} takes {model.__name__}, not {', '.join(sorted(strays))}")
        data = pa.Table.from_pylist([row.model_dump() for row in rows], schema=schema_for(model))
        sink = pa.BufferOutputStream()
        # pyarrow-stubs leaves a few of these two functions' parameter types unknown.
        pq.write_table(data, sink)  # pyright: ignore[reportUnknownMemberType]
        path = self.path(table, game_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, sink.getvalue().to_pybytes())
        return path

    def read(self, table: str, game_date: date) -> pa.Table:
        """The date's file for `table`; raises `FileNotFoundError` if it was never written, and
        `ParquetSchemaError` if it was written from a different row model."""
        data = pq.read_table(self.path(table, game_date))  # pyright: ignore[reportUnknownMemberType]
        expected = schema_for(ROW_MODELS[table])
        if not data.schema.equals(expected, check_metadata=False):
            raise ParquetSchemaError(
                f"{self.path(table, game_date)} has schema {data.schema}, not {expected}"
            )
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
