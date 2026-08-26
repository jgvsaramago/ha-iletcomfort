"""Tests for the Aquapura Split Green "Eco"/"Disparo" mode select entity.

The Silent Mode select (ILetComfortMuteSelect) already has this behavior
(unconditional registration, silent no-op on profiles that ignore its kwarg);
these tests cover the newer ILetComfortAquapuraSplitGreenModeSelect the same
way.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from custom_components.iletcomfort.api import ITSStatus
from custom_components.iletcomfort.select import (
    AQUAPURA_SPLIT_GREEN_MODE_OPTIONS,
    ILetComfortAquapuraSplitGreenModeSelect,
)


def _select(status: ITSStatus | None):
    coordinator = MagicMock()
    coordinator.appliance_code = "APPL1"
    coordinator.data = {"status": status} if status is not None else None
    coordinator.async_set_device = AsyncMock()
    return ILetComfortAquapuraSplitGreenModeSelect(coordinator)


def test_options_are_eco_and_disparo():
    assert AQUAPURA_SPLIT_GREEN_MODE_OPTIONS == ["Eco", "Disparo"]


def test_current_option_reads_confirmed_status_value():
    entity = _select(ITSStatus(operating_mode="Disparo"))
    assert entity.current_option == "Disparo"

    entity = _select(ITSStatus(operating_mode="Eco"))
    assert entity.current_option == "Eco"


def test_current_option_none_when_no_data():
    assert _select(None).current_option is None


def test_current_option_none_for_unconfirmed_marker():
    """An "Unknown(0xNN)" marker (or None, e.g. another profile) reads as None
    rather than being forced into one of the two known options.
    """
    entity = _select(ITSStatus(operating_mode="Unknown(0x20)"))
    assert entity.current_option is None

    entity = _select(ITSStatus())  # operating_mode defaults to None
    assert entity.current_option is None


async def test_async_select_option_forwards_operating_mode():
    entity = _select(ITSStatus(operating_mode="Eco"))
    await entity.async_select_option("Disparo")
    entity.coordinator.async_set_device.assert_awaited_once_with(
        operating_mode="Disparo",
    )
