from pathlib import Path

import pytest

from closinglineup.names import NameIndex, first_last, load_aliases, normalize


def test_normalize_ignores_case_accents_and_punctuation() -> None:
    assert normalize("Jusuf Nurkić") == normalize("jusuf nurkic") == "jusuf nurkic"
    assert normalize("T.J. McConnell") == "tj mcconnell"
    assert normalize("De'Aaron Fox") == "deaaron fox"
    assert normalize("Keita Bates-Diop") == "keita bates diop"


def test_first_last_turns_a_report_name_round() -> None:
    assert first_last("Lively II, Dereck") == "Dereck Lively II"
    assert first_last("Niederhauser, Yanic Konan") == "Yanic Konan Niederhauser"
    assert first_last("Nene") == "Nene"


def test_resolve_matches_the_normalized_name_and_prefers_an_alias() -> None:
    index = NameIndex([(1, "Jusuf Nurkić"), (2, "Yang Hansen")], {"hansen yang": 2})

    assert index.resolve("Jusuf Nurkic") == 1
    assert index.resolve("Hansen Yang") == 2
    assert index.resolve("Tamar Bates") is None


def test_resolve_settles_a_shared_name_by_the_team_roster() -> None:
    index = NameIndex([(1, "Brandon Williams"), (2, "Brandon Williams")], {})

    assert index.resolve("Brandon Williams") is None
    assert index.resolve("Brandon Williams", {2, 99}) == 2
    assert index.resolve("Brandon Williams", {1, 2}) is None


def test_the_alias_table_loads() -> None:
    assert load_aliases()["hansen yang"] == 1642905


def test_an_alias_table_that_is_not_names_to_ids_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "aliases.yml"
    path.write_text("Hansen Yang: not an id\n")

    with pytest.raises(ValueError, match="is not a name and a player ID"):
        load_aliases(path)
