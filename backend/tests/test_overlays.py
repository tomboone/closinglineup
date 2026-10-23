import pytest

from closinglineup.overlays import (
    Overlay,
    UnknownOverlayError,
    by_name,
    overlays_in,
    register_overlay,
)
from tests.fakes import FakeOverlay


def test_overlays_in_a_family_come_in_registration_order(registry: dict[str, Overlay]) -> None:
    transactions, contracts, epm = FakeOverlay(), FakeOverlay(), FakeOverlay()
    register_overlay("transactions", "daily", transactions)
    register_overlay("contracts", "weekly", contracts)
    register_overlay("epm", "daily", epm)

    assert [overlay.name for overlay in overlays_in("daily")] == ["transactions", "epm"]
    assert by_name("contracts") == Overlay("contracts", "weekly", contracts)


def test_a_name_registers_once(registry: dict[str, Overlay]) -> None:
    register_overlay("epm", "daily", FakeOverlay())

    with pytest.raises(ValueError, match="'epm' is already registered"):
        register_overlay("epm", "weekly", FakeOverlay())


def test_an_unknown_name_lists_those_registered(registry: dict[str, Overlay]) -> None:
    register_overlay("epm", "daily", FakeOverlay())

    with pytest.raises(UnknownOverlayError, match="no overlay named 'darko'; registered: epm"):
        by_name("darko")
