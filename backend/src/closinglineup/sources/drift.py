"""Schema drift: a response whose shape is not the one its endpoint declares (spec §5, handoff §10).

Each endpoint function declares the shape it expects: a v2-shaped stats.nba.com response by its
result sets' names and columns (`ResultSets`), a v3 or cdn.nba.com one by the top-level keys its
data is under (`TopLevel`). `fetch_json` compares a response with it before checking it for data or
caching it, and raises `SchemaDrift` if a declared name is missing, so a renamed column is reported
rather than parsed into wrong rows. The difference names the missing names and, beside them, those
the shape does not declare, a renamed column's new name among them; a name added alone is not drift,
since nothing reads it. A drifted response is not cached, as a bad one is not: a game whose
stats.nba.com response drifted is `stats_failed` and parsed from cdn.nba.com's live box (spec §4.2),
and `fetch --missing` fetches it again once its declaration and parser follow. A response read back
from the cache is not compared again.

A column moved within its result set is not drift: the parsers read columns by name. The shapes
are those of the recorded fixtures under `tests/fixtures/`, which `tests/test_drift.py` holds them
to, so re-recording a fixture whose shape changed fails until the declaration follows.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

type Payload = dict[str, Any]


def _differences(what: str, expected: set[str], found: set[str]) -> tuple[list[str], list[str]]:
    """The names of `expected` not `found`, and those `found` not expected, each as a line."""
    missing = [f"missing {what} {name}" for name in sorted(expected - found)]
    unexpected = [f"unexpected {what} {name}" for name in sorted(found - expected)]
    return missing, unexpected


def _drift(*differences: tuple[list[str], list[str]]) -> list[str]:
    """Every line of `differences`, if any of them has a name missing; else none."""
    if not any(missing for missing, _ in differences):
        return []
    return [line for missing, unexpected in differences for line in [*missing, *unexpected]]


@dataclass(frozen=True, slots=True)
class ResultSets:
    """A v2 response's shape: each result set's name, and its columns."""

    sets: Mapping[str, frozenset[str]]

    def diff(self, payload: Payload) -> list[str]:
        """Each way `payload` differs from the shape, if it has drifted; else none."""
        found_sets: list[Payload] = payload.get("resultSets") or []
        found: dict[str, set[str]] = {
            result_set.get("name", ""): set[str](result_set.get("headers") or [])
            for result_set in found_sets
        }
        return _drift(
            _differences("result set", set(self.sets), set(found)),
            *(
                _differences(f"{name} column", set(columns), found[name])
                for name, columns in self.sets.items()
                if name in found
            ),
        )


@dataclass(frozen=True, slots=True)
class TopLevel:
    """A v3 or cdn.nba.com response's shape: the top-level keys its data is under."""

    keys: frozenset[str]

    def diff(self, payload: Payload) -> list[str]:
        """Each way `payload` differs from the shape, if it has drifted; else none."""
        return _drift(_differences("key", set(self.keys), set(payload)))


type Shape = ResultSets | TopLevel


def result_sets(**sets: str) -> ResultSets:
    """A `ResultSets` from each result set's columns, written space-separated."""
    return ResultSets({name: frozenset(columns.split()) for name, columns in sets.items()})


def top_level(*keys: str) -> TopLevel:
    return TopLevel(frozenset(keys))


class SchemaDrift(Exception):
    """A response, not cached, whose shape differs from its endpoint's declared one.

    `differences` lists each way it differs, e.g. `missing PlayerStats column MIN`.
    """

    def __init__(self, source: str, endpoint: str, key: str, differences: list[str]) -> None:
        super().__init__(f"{source}/{endpoint} {key}: schema drift: {'; '.join(differences)}")
        self.source = source
        self.endpoint = endpoint
        self.key = key
        self.differences = differences
