"""Tests for shared device grouping and the ODU Current sensor (issue #10)."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import ITSSensors, ITSStatus
from custom_components.iletcomfort.binary_sensor import (
    BINARY_SENSOR_DESCRIPTIONS,
    ILetComfortBinarySensor,
)
from custom_components.iletcomfort.climate import ILetComfortClimate
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DOMAIN,
    REGION_US,
)
from custom_components.iletcomfort.coordinator import ILetComfortCoordinator
from custom_components.iletcomfort.model_profiles import AquapuraSplitGreenScheduleSlot
from custom_components.iletcomfort.select import ILetComfortMuteSelect
from custom_components.iletcomfort.sensor import (
    SENSOR_DESCRIPTIONS,
    ILetComfortSensor,
)
from custom_components.iletcomfort.switch import ILetComfortBoostSwitch


def _coordinator(hass: HomeAssistant) -> ILetComfortCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Pool Heat Pump",
        unique_id="user@example.com:APPL1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_US,
        },
        version=2,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord = ILetComfortCoordinator(hass, entry)
    coord.data = {
        "status": ITSStatus(mode=1),
        "sensors": ITSSensors(odu_current=4.0, odu_version="1.2.3"),
    }
    return coord


def test_all_platforms_share_one_device(hass: HomeAssistant):
    """Every platform's entity must attach to the same Device by appliance_code."""
    coord = _coordinator(hass)
    entities = [
        ILetComfortSensor(coord, SENSOR_DESCRIPTIONS[0]),
        ILetComfortBinarySensor(coord, BINARY_SENSOR_DESCRIPTIONS[0]),
        ILetComfortClimate(coord),
        ILetComfortBoostSwitch(coord),
        ILetComfortMuteSelect(coord),
    ]

    expected_identifiers = {(DOMAIN, "APPL1")}
    for ent in entities:
        assert ent.device_info is not None
        assert ent.device_info["identifiers"] == expected_identifiers


def test_device_info_uses_entry_title_and_firmware(hass: HomeAssistant):
    """Device name comes from the entry title; sw_version from odu firmware."""
    coord = _coordinator(hass)
    info = ILetComfortSensor(coord, SENSOR_DESCRIPTIONS[0]).device_info

    assert info is not None
    assert info.get("name") == "Pool Heat Pump"
    assert info.get("manufacturer") == "iLetComfort"
    assert info.get("sw_version") == "1.2.3"


def test_odu_current_sensor_exists_and_reads_scaled_amps(hass: HomeAssistant):
    """The ODU Current sensor (issue #10/#11) must expose the scaled Ampere value.

    odu_current is decoded as fixed-point Amperes (raw / 256), so the sensor
    surfaces the physical value directly rather than the raw 16-bit count.
    """
    coord = _coordinator(hass)
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "odu_current")

    sensor = ILetComfortSensor(coord, desc)
    assert sensor.native_value == 4.0


def test_daily_schedule_sensors_read_the_named_slot(hass: HomeAssistant):
    """Daily Schedule N {Setpoint,Start Time,End Time,Mode} read slot N-1."""
    coord = _coordinator(hass)
    coord.data["schedule"] = [
        AquapuraSplitGreenScheduleSlot(
            active=True, mode="Eco", setpoint=50.0,
            start_time="09:00", end_time="21:00",
        ),
        AquapuraSplitGreenScheduleSlot(
            active=False, mode="Eco", setpoint=60.0,
            start_time="14:00", end_time="18:00",
        ),
    ]

    def _value(key: str):
        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
        return ILetComfortSensor(coord, desc).native_value

    assert _value("daily_schedule_1_setpoint") == 50.0
    assert _value("daily_schedule_1_start_time") == "09:00"
    assert _value("daily_schedule_1_end_time") == "21:00"
    assert _value("daily_schedule_1_mode") == "Eco"
    assert _value("daily_schedule_2_setpoint") == 60.0
    # Slot 3/4 aren't in this poll's data (only 2 supplied above) — must read
    # None rather than crash on the out-of-range index.
    assert _value("daily_schedule_3_setpoint") is None
    assert _value("daily_schedule_4_start_time") is None


def test_daily_schedule_sensors_none_without_schedule_data(hass: HomeAssistant):
    """No schedule key (STANDARD/other profiles) → every field reads None."""
    coord = _coordinator(hass)
    assert "schedule" not in coord.data

    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "daily_schedule_1_setpoint")
    assert ILetComfortSensor(coord, desc).native_value is None


def test_daily_schedule_active_binary_sensors_read_the_named_slot(
    hass: HomeAssistant,
):
    """Daily Schedule N Active reflects slot N-1's active flag."""
    coord = _coordinator(hass)
    coord.data["schedule"] = [
        AquapuraSplitGreenScheduleSlot(active=True),
        AquapuraSplitGreenScheduleSlot(active=False),
    ]

    def _is_on(key: str) -> bool:
        desc = next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == key)
        return ILetComfortBinarySensor(coord, desc).is_on

    assert _is_on("daily_schedule_1_active") is True
    assert _is_on("daily_schedule_2_active") is False
    # Out-of-range / missing slot → False, not a crash.
    assert _is_on("daily_schedule_3_active") is False


def test_daily_schedule_active_binary_sensor_false_without_schedule_data(
    hass: HomeAssistant,
):
    """No schedule key (STANDARD/other profiles) → Active reads False."""
    coord = _coordinator(hass)
    assert "schedule" not in coord.data

    desc = next(
        d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == "daily_schedule_1_active"
    )
    assert ILetComfortBinarySensor(coord, desc).is_on is False
