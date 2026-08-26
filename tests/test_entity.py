"""Tests for shared device grouping and the ODU Current sensor (issue #10)."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import ITSSensors, ITSStatus
from custom_components.iletcomfort import binary_sensor as binary_sensor_platform
from custom_components.iletcomfort import sensor as sensor_platform
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
from custom_components.iletcomfort.model_profiles import (
    AquapuraSplitGreenConsumption,
    AquapuraSplitGreenDisinfectionSettings,
    AquapuraSplitGreenScheduleSlot,
)
from custom_components.iletcomfort import select as select_platform
from custom_components.iletcomfort.select import ILetComfortMuteSelect
from custom_components.iletcomfort.sensor import (
    SENSOR_DESCRIPTIONS,
    ILetComfortSensor,
)
from custom_components.iletcomfort.switch import (
    ILetComfortBoostSwitch,
    ILetComfortDisinfectionSwitch,
    ILetComfortForceDisinfectionSwitch,
    ILetComfortHeatingElementSwitch,
    ILetComfortSilenceSwitch,
)


def _coordinator(
    hass: HomeAssistant, options: dict | None = None,
) -> ILetComfortCoordinator:
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
        options=options or {},
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
        ILetComfortSilenceSwitch(coord),
        ILetComfortDisinfectionSwitch(coord),
        ILetComfortHeatingElementSwitch(coord),
        ILetComfortForceDisinfectionSwitch(coord),
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


def test_internal_diagnostic_entities_are_categorized_diagnostic():
    """Internal-circuit/electrical/runtime entities group under the device
    page's collapsed "Diagnostic" section, matching what other HA integrations
    do for values that aren't everyday-useful (HA groups
    entity_category=DIAGNOSTIC entities separately from the main entity list).
    """
    for key in (
        "condenser", "evaporator", "refrigerant", "plate_hx",
        "compressor_freq", "comp_run_hours", "pressure_high", "pressure_low",
        "error_code", "odu_voltage", "odu_current",
    ):
        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
        assert desc.entity_category is EntityCategory.DIAGNOSTIC, key

    for key in ("ibh_running", "error"):
        desc = next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == key)
        assert desc.entity_category is EntityCategory.DIAGNOSTIC, key


def test_primary_entities_are_not_diagnostic():
    """Everyday-relevant values stay in the main entity list, not tucked away
    under Diagnostic: live water/outdoor temps, total energy (for the Energy
    dashboard), and compressor running.
    """
    for key in (
        "water_inlet", "water_outlet", "dhw_tank", "outdoor_ambient", "total_energy",
    ):
        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
        assert desc.entity_category is None, key

    desc = next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == "compressor_running")
    assert desc.entity_category is None, "compressor_running"


def test_daily_schedule_entities_are_categorized_diagnostic():
    """The daily-schedule entities represent the device's own timer
    configuration, but must use entity_category=DIAGNOSTIC, not CONFIG:
    HA's SensorEntity/BinarySensorEntity reject CONFIG at add-time with a
    HomeAssistantError ("cannot be added as the entity category is set to
    config") since CONFIG is reserved for entities the user can change
    (switch/number/select), not read-only sensors.
    """
    for key in (
        "daily_schedule_1_setpoint", "daily_schedule_1_start_time",
        "daily_schedule_1_end_time", "daily_schedule_1_mode",
    ):
        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
        assert desc.entity_category is EntityCategory.DIAGNOSTIC, key

    desc = next(
        d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == "daily_schedule_1_active"
    )
    assert desc.entity_category is EntityCategory.DIAGNOSTIC, "daily_schedule_1_active"


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


def test_disinfection_sensors_read_settings(hass: HomeAssistant):
    """Disinfection Temperature/Time/Cycle read coordinator.data["disinfection"]."""
    coord = _coordinator(hass)
    coord.data["disinfection"] = AquapuraSplitGreenDisinfectionSettings(
        enabled=True, hour=14, minute=0, temperature=65.0, cycle_days=7,
    )

    def _value(key: str):
        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
        return ILetComfortSensor(coord, desc).native_value

    assert _value("disinfection_temperature") == 65.0
    assert _value("disinfection_time") == "14:00"
    assert _value("disinfection_cycle_days") == 7


def test_disinfection_sensors_none_without_disinfection_data(hass: HomeAssistant):
    """No disinfection key (STANDARD/other profiles) → every field reads None."""
    coord = _coordinator(hass)
    assert "disinfection" not in coord.data

    for key in (
        "disinfection_temperature", "disinfection_time", "disinfection_cycle_days",
    ):
        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
        assert ILetComfortSensor(coord, desc).native_value is None


def test_consumption_sensors_read_data(hass: HomeAssistant):
    """Day/Week/Month/Year Energy read coordinator.data["consumption"]; Total
    Energy prefers it over the generic status.total_kwh.
    """
    coord = _coordinator(hass)
    coord.data["consumption"] = AquapuraSplitGreenConsumption(
        day=1.12, week=4.90, month=4.90, year=4.90, total=4.90,
    )

    def _value(key: str):
        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
        return ILetComfortSensor(coord, desc).native_value

    assert _value("day_energy") == 1.12
    assert _value("week_energy") == 4.90
    assert _value("month_energy") == 4.90
    assert _value("year_energy") == 4.90
    assert _value("total_energy") == 4.90


def test_consumption_sensors_none_without_consumption_data(hass: HomeAssistant):
    """No consumption key (STANDARD/other profiles) → day/week/month/year read
    None, and Total Energy falls back to the generic status.total_kwh.
    """
    coord = _coordinator(hass)
    assert "consumption" not in coord.data
    coord.data["status"] = ITSStatus(mode=1, total_kwh=42)

    def _value(key: str):
        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
        return ILetComfortSensor(coord, desc).native_value

    for key in ("day_energy", "week_energy", "month_energy", "year_energy"):
        assert _value(key) is None
    assert _value("total_energy") == 42


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


# --- Daily Schedule entities: always registered, like every other entity in
# this integration (no options flow to disable their fetch). ----------------


async def test_daily_schedule_entities_are_always_registered(hass: HomeAssistant):
    coord = _coordinator(hass)
    hass.data.setdefault(DOMAIN, {})[coord.entry.entry_id] = coord

    added = []
    await sensor_platform.async_setup_entry(hass, coord.entry, added.extend)
    keys = {e.entity_description.key for e in added}
    assert "daily_schedule_1_setpoint" in keys
    assert "daily_schedule_4_mode" in keys
    assert "dhw_tank" in keys
    assert "odu_current" in keys

    added_bs = []
    await binary_sensor_platform.async_setup_entry(hass, coord.entry, added_bs.extend)
    bs_keys = {e.entity_description.key for e in added_bs}
    assert "daily_schedule_1_active" in bs_keys
    assert "compressor_running" in bs_keys


# --- Silent Mode select: hidden for profiles whose set_device() bypasses the
# ctrl_flag/mute_level write entirely (mute would silently do nothing). ------


async def test_silent_mode_select_registered_for_standard_profile(
    hass: HomeAssistant,
):
    coord = _coordinator(hass)  # no appliance_meta -> sn8 None -> STANDARD
    hass.data.setdefault(DOMAIN, {})[coord.entry.entry_id] = coord

    added = []
    await select_platform.async_setup_entry(hass, coord.entry, added.extend)
    assert len(added) == 1
    assert isinstance(added[0], ILetComfortMuteSelect)


async def test_silent_mode_select_absent_for_aquapura_split_green(
    hass: HomeAssistant,
):
    coord = _coordinator(hass)
    coord.appliance_meta = {"sn8": "17186T3A"}
    hass.data.setdefault(DOMAIN, {})[coord.entry.entry_id] = coord

    added = []
    await select_platform.async_setup_entry(hass, coord.entry, added.extend)
    assert added == []


async def test_silent_mode_select_absent_for_kjrh120l(hass: HomeAssistant):
    coord = _coordinator(hass)
    coord.appliance_meta = {"sn8": "17100003"}
    hass.data.setdefault(DOMAIN, {})[coord.entry.entry_id] = coord

    added = []
    await select_platform.async_setup_entry(hass, coord.entry, added.extend)
    assert added == []


async def test_full_integration_setup_adds_every_entity_without_error(
    hass: HomeAssistant, caplog,
):
    """Regression test for the entity_category=CONFIG bug: HA's
    SensorEntity/BinarySensorEntity raise HomeAssistantError ("cannot be
    added as the entity category is set to config") if entity_category is
    CONFIG, since that's reserved for entities the user can change. Every
    other test in this file collects entity objects into a plain list and
    never runs them through HA's real add-to-platform validation, so this
    class of bug was invisible to the test suite. This one goes through
    ``hass.config_entries.async_setup`` so ``add_to_platform_finish()`` (and
    the entity_category check inside it) genuinely runs for every entity,
    for a profile (Aquapura Split Green) that populates every optional field
    (schedule, disinfection) so every description actually gets exercised.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
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

    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        client = mock_cls.return_value
        client.load_token.return_value = True
        client.list_appliances.return_value = [
            {"applianceCode": "APPL1", "sn8": "17186T3A"},
        ]
        client.query_status.return_value = ITSStatus(mode=1)
        client.query_sensors.return_value = ITSSensors()
        client.query_daily_schedule.return_value = [
            AquapuraSplitGreenScheduleSlot(
                active=True, mode="Eco", setpoint=50.0,
                start_time="09:00", end_time="21:00",
            ),
        ]
        client.query_disinfection.return_value = AquapuraSplitGreenDisinfectionSettings(
            enabled=True, hour=14, minute=0, temperature=65.0, cycle_days=7,
        )
        client.query_heating_element.return_value = True
        client.query_force_disinfection.return_value = False
        client.query_consumption.return_value = AquapuraSplitGreenConsumption(
            day=1.12, week=4.90, month=4.90, year=4.90, total=4.90,
        )

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert "cannot be added as the entity category" not in caplog.text
    assert "Error adding entity" not in caplog.text

    entity_ids = hass.states.async_entity_ids()
    assert any("daily_schedule_1_setpoint" in e for e in entity_ids)
    assert any("daily_schedule_1_active" in e for e in entity_ids)
    assert any(e.startswith("switch.") and "heating_element" in e for e in entity_ids)
    assert any("disinfection_temperature" in e for e in entity_ids)
    assert any(e.startswith("switch.") and "disinfection" in e for e in entity_ids)
    assert any(e.startswith("switch.") and "force_disinfection" in e for e in entity_ids)
    assert any("day_energy" in e for e in entity_ids)
