"""Tests for the EXPERIMENTAL KJRH-120L dual-variant DHW setpoint number (#5).

sn8 17100003 fronts two unit types. Only the *dual* heating variant (status
body[8]==1 and body[9]==1) gets a DHW setpoint number entity; the pure-DHW unit
(body[8]/[9]==0) and every other model get no number entity. The number reads
the DHW setpoint from status body[15] and writes it with the confirmed DHW
command (field 0x07).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.iletcomfort.api import ITSSensors, ITSStatus
from custom_components.iletcomfort.number import (
    ILetComfortKjrh120lDhwSetpoint,
    _is_kjrh120l_dual,
)
from custom_components.iletcomfort.model_profiles import (
    ATW_SN8,
    KJRH120L_SN8,
    decode_kjrh120l_status,
)

_KJRH_DUAL_ROOM19_DHW51 = bytes(
    int(x, 16)
    for x in ("01,fe,00,00,00,41,00,55,01,01,01,02,13,0c,30,33,00,00,00,00").split(",")
)
_KJRH_PURE_DHW_OFF = bytes(
    int(x, 16)
    for x in (
        "01,fe,00,00,00,42,00,56,00,00,00,03,41,1e,30,3c,00,00,00,00,00,00,01,"
        "00,01,00,00,00,01,00,00,00,00,00,01,02,02,4b,23,19,05,37,19,19,05,3c,"
        "22,46,14,13,00,01,01,02,03,01,01,e7,2f,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,"
        "ff,30,ff,ff,ff,ff,00,00,00,01,00,00,00,00,00,17,07,14,0f,20,00,00,00,"
        "00,00,ff"
    ).split(",")
)


def _coordinator(sn8: str | None, status: ITSStatus | None):
    coordinator = MagicMock()
    coordinator.appliance_code = "APPL1"
    coordinator.sn8 = sn8
    coordinator.appliance_meta = {"sn8": sn8} if sn8 else None
    coordinator.data = {"status": status, "sensors": ITSSensors()}
    coordinator.async_set_device = AsyncMock()
    return coordinator


def test_is_dual_true_for_dual_frame():
    status = decode_kjrh120l_status(_KJRH_DUAL_ROOM19_DHW51)
    assert _is_kjrh120l_dual(_coordinator(KJRH120L_SN8, status)) is True


def test_is_dual_false_for_pure_dhw_frame():
    status = decode_kjrh120l_status(_KJRH_PURE_DHW_OFF)
    assert _is_kjrh120l_dual(_coordinator(KJRH120L_SN8, status)) is False


@pytest.mark.parametrize("sn8", [None, ATW_SN8])
def test_is_dual_false_for_non_kjrh(sn8):
    assert _is_kjrh120l_dual(_coordinator(sn8, ITSStatus())) is False


def test_dhw_number_reads_body15():
    """The DHW number reads status.kjrh120l_dhw_setpoint (body[15]=0x33=51)."""
    status = decode_kjrh120l_status(_KJRH_DUAL_ROOM19_DHW51)
    entity = ILetComfortKjrh120lDhwSetpoint(_coordinator(KJRH120L_SN8, status))
    assert entity.native_value == 51.0


def test_dhw_number_range_is_35_to_60():
    status = decode_kjrh120l_status(_KJRH_DUAL_ROOM19_DHW51)
    entity = ILetComfortKjrh120lDhwSetpoint(_coordinator(KJRH120L_SN8, status))
    assert entity.native_min_value == 35.0
    assert entity.native_max_value == 60.0


async def test_dhw_number_writes_dhw_command():
    """Setting the number sends temperature= (DHW field 0x07), not room_temperature."""
    status = decode_kjrh120l_status(_KJRH_DUAL_ROOM19_DHW51)
    entity = ILetComfortKjrh120lDhwSetpoint(_coordinator(KJRH120L_SN8, status))
    await entity.async_set_native_value(55)
    entity.coordinator.async_set_device.assert_awaited_once_with(temperature=55)


async def test_dhw_number_clamps_to_range():
    status = decode_kjrh120l_status(_KJRH_DUAL_ROOM19_DHW51)
    entity = ILetComfortKjrh120lDhwSetpoint(_coordinator(KJRH120L_SN8, status))
    await entity.async_set_native_value(99)
    sent = entity.coordinator.async_set_device.call_args.kwargs["temperature"]
    assert sent == 60
