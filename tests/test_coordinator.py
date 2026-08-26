"""Tests for the iLetComfort DataUpdateCoordinator wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from datetime import timedelta

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import ApiError, ITSSensors, ITSStatus
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    REGION_EU,
    REGION_US,
)
from custom_components.iletcomfort.coordinator import (
    CONFIG_FETCH_INTERVAL,
    OFFLINE_REPAIR_ID,
    OFFLINE_REPAIR_THRESHOLD,
    SUSTAINED_FAILURE_THRESHOLD,
    ILetComfortCoordinator,
)


def _entry(region: str | None, options: dict | None = None) -> MockConfigEntry:
    data = {
        CONF_EMAIL: "user@example.com",
        CONF_PASSWORD: "secret",
        CONF_APPLIANCE_CODE: "APPL1",
    }
    if region is not None:
        data[CONF_REGION] = region
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"user@example.com:APPL1",
        data=data,
        options=options or {},
        version=2,
    )


async def test_coordinator_us_region_routes_to_us_dollin(hass: HomeAssistant):
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        ILetComfortCoordinator(hass, entry)

    mock_cls.assert_called_once()
    assert mock_cls.call_args.kwargs["api_base"] == "https://us.dollin.net"


async def test_coordinator_eu_region_routes_to_eu_dollin(hass: HomeAssistant):
    entry = _entry(REGION_EU)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        ILetComfortCoordinator(hass, entry)

    assert mock_cls.call_args.kwargs["api_base"] == "https://eu.dollin.net"


async def test_coordinator_defaults_to_us_when_region_missing(
    hass: HomeAssistant,
):
    """Legacy v1 entries with no CONF_REGION should still resolve to US."""
    entry = _entry(region=None)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        ILetComfortCoordinator(hass, entry)

    assert mock_cls.call_args.kwargs["api_base"] == "https://us.dollin.net"


async def test_token_file_is_scoped_per_entry(hass: HomeAssistant):
    """The token file path must include the entry_id so multi-entry doesn't collide."""
    entry_a = _entry(REGION_US)
    entry_b = _entry(REGION_US)
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord_a = ILetComfortCoordinator(hass, entry_a)
        coord_b = ILetComfortCoordinator(hass, entry_b)

    # Different entries → different token files (entry_id is in the name).
    assert coord_a._token_file != coord_b._token_file
    assert entry_a.entry_id in str(coord_a._token_file)
    assert entry_b.entry_id in str(coord_b._token_file)
    assert coord_a._token_file.name.startswith("iletcomfort_token_")


async def test_poll_falls_back_to_cache_on_truncated_frame(hass: HomeAssistant):
    """A truncated-frame ApiError must keep cached data, not blank the entities.

    Issue #5: the device intermittently returns empty frames; the coordinator
    should preserve the last good ITSStatus/ITSSensors rather than overwriting
    them with all-defaults.
    """
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    cached_status = ITSStatus(mode=1)
    cached_sensors = ITSSensors()
    coord.data = {"status": cached_status, "sensors": cached_sensors}

    client.query_status.side_effect = ApiError("truncated frame")
    client.query_sensors.side_effect = ApiError("truncated frame")

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    assert result["status"] is cached_status
    assert result["sensors"] is cached_sensors


async def test_single_transient_failure_logs_debug_not_warning(
    hass: HomeAssistant, caplog
):
    """A one-off cloud/DNS blip (fail then success) must stay at DEBUG.

    Issue #44: on a flaky vendor cloud an isolated 502/RemoteDisconnected/DNS
    failure is expected and harmless (the poll falls back to cache and recovers
    next time), so it must not surface a WARNING.
    """
    import logging

    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    coord.data = {"status": ITSStatus(mode=1), "sensors": ITSSensors()}

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        # One failing poll ...
        client.query_status.side_effect = ApiError("502 Bad Gateway")
        client.query_sensors.side_effect = ApiError("502 Bad Gateway")
        with caplog.at_level(logging.DEBUG):
            await coord._poll()

        # ... immediately followed by a healthy one.
        client.query_status.side_effect = None
        client.query_status.return_value = ITSStatus(mode=1)
        client.query_sensors.side_effect = None
        client.query_sensors.return_value = ITSSensors()
        await coord._poll()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings
    debug_fallbacks = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "using cache" in r.getMessage()
    ]
    assert len(debug_fallbacks) == 2  # status + sensors, both at DEBUG


async def test_intermittent_failures_never_warn(hass: HomeAssistant, caplog):
    """fail → success → fail → success must never escalate to WARNING.

    Issue #44: the old "warn on entry to degraded, reset on success" logic
    re-warned on every fresh failure, so an intermittent flaky cloud produced
    hundreds of WARNINGs. Because success resets the streak, no query ever
    reaches the sustained threshold here.
    """
    import logging

    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    coord.data = {"status": ITSStatus(mode=1), "sensors": ITSSensors()}
    good_status = ITSStatus(mode=1)
    good_sensors = ITSSensors()

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        with caplog.at_level(logging.WARNING):
            for _ in range(6):
                # Fail this poll.
                client.query_status.side_effect = ApiError("code=1214, msg=System error")
                client.query_sensors.side_effect = ApiError("RemoteDisconnected")
                await coord._poll()
                # Recover next poll.
                client.query_status.side_effect = None
                client.query_status.return_value = good_status
                client.query_sensors.side_effect = None
                client.query_sensors.return_value = good_sensors
                await coord._poll()

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


async def test_sustained_failures_warn_once_at_threshold_then_debug(
    hass: HomeAssistant, caplog
):
    """SUSTAINED_FAILURE_THRESHOLD consecutive failures warn exactly once.

    Issue #44: DEBUG for every poll below the threshold, a single WARNING at
    the threshold poll, then DEBUG again for the ongoing sustained failure so
    a genuinely stuck state is flagged once without flooding the log.
    """
    import logging

    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    coord.data = {"status": ITSStatus(mode=1), "sensors": ITSSensors()}
    client.query_status.side_effect = ApiError("502 Bad Gateway")
    client.query_sensors.side_effect = ApiError("502 Bad Gateway")

    total_polls = SUSTAINED_FAILURE_THRESHOLD + 2
    warning_polls: list[int] = []

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        with caplog.at_level(logging.DEBUG):
            for poll in range(1, total_polls + 1):
                caplog.clear()
                await coord._poll()
                if [r for r in caplog.records if r.levelno == logging.WARNING]:
                    warning_polls.append(poll)

    # Exactly one poll warned, and it was the threshold poll — one WARNING per
    # query (status + sensors) on that poll only.
    assert warning_polls == [SUSTAINED_FAILURE_THRESHOLD]


async def test_recovery_after_sustained_logs_info_once(hass: HomeAssistant, caplog):
    """Recovering out of the sustained-WARNING state logs a single INFO."""
    import logging

    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    coord.data = {"status": ITSStatus(mode=1), "sensors": ITSSensors()}
    client.query_status.side_effect = ApiError("502 Bad Gateway")
    client.query_sensors.side_effect = ApiError("502 Bad Gateway")

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        for _ in range(SUSTAINED_FAILURE_THRESHOLD):
            await coord._poll()

        client.query_status.side_effect = None
        client.query_status.return_value = ITSStatus(mode=1)
        client.query_sensors.side_effect = None
        client.query_sensors.return_value = ITSSensors()
        with caplog.at_level(logging.INFO):
            await coord._poll()

    recovered = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "recovered" in r.getMessage()
    ]
    assert len(recovered) == 2  # one each for status + sensors


async def test_first_refresh_populates_appliance_meta_by_code(hass: HomeAssistant):
    """async_first_refresh_with_login caches the appliance whose code matches.

    Diagnostic-only metadata (issue #22): given a mocked list_appliances the
    coordinator stores the dict whose ``applianceCode`` equals appliance_code.
    """
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    client.load_token.return_value = True  # skip login path
    matching = {
        "applianceCode": "APPL1",
        "applianceType": "0xC3",
        "modelNumber": "0",
        "sn8": "171H120F",
        "owner": "someone@example.com",
        "sn": "SECRETSN",
        "name": "Living Room",
        "online": "1",
    }
    other = {"applianceCode": "OTHER", "applianceType": "0x00"}
    client.list_appliances.return_value = [other, matching]

    with patch.object(
        ILetComfortCoordinator, "async_config_entry_first_refresh", new=AsyncMock()
    ):
        await coord.async_first_refresh_with_login()

    assert coord.appliance_meta == matching


async def test_ensure_appliance_meta_failure_leaves_none_and_does_not_block(
    hass: HomeAssistant,
):
    """A list_appliances error must not blank metadata-collection nor block refresh."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    client.load_token.return_value = True  # skip login path
    client.list_appliances.side_effect = ApiError("boom")

    first_refresh = AsyncMock()
    with patch.object(
        ILetComfortCoordinator,
        "async_config_entry_first_refresh",
        new=first_refresh,
    ):
        await coord.async_first_refresh_with_login()

    assert coord.appliance_meta is None
    first_refresh.assert_awaited_once()


async def test_sn8_property_reads_appliance_meta(hass: HomeAssistant):
    """The coordinator exposes the appliance sn8 used to select a decode profile."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord = ILetComfortCoordinator(hass, entry)

    assert coord.sn8 is None  # no metadata yet
    coord.appliance_meta = {"sn8": "171H120F"}
    assert coord.sn8 == "171H120F"
    coord.appliance_meta = {"sn8": ""}
    assert coord.sn8 is None


async def test_poll_passes_sn8_and_applies_atw_overrides(hass: HomeAssistant):
    """An ATW (sn8 171H120F) poll passes sn8 to query_status and routes the
    DHW tank temp into th_temp (the "DHW Tank Temperature" sensor) while leaving
    twin_temp (Water Inlet) honest."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    coord.appliance_meta = {"sn8": "171H120F"}
    client = mock_cls.return_value
    # The client already applies the ATW status profile, so its query_status
    # returns box_bottom_temp=46 with twin_temp still 0 from the sensors decode.
    atw_status = ITSStatus(box_bottom_temp=46.0, set_temperature=50, t5s_def=21.0)
    client.query_status.return_value = atw_status
    client.query_sensors.return_value = ITSSensors(twin_temp=0.0)

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    # sn8 must be forwarded to both queries (so KJRH-120L gets the short cmd).
    assert client.query_status.call_args.args == ("APPL1", "171H120F")
    assert client.query_sensors.call_args.args == ("APPL1", "171H120F")
    # th_temp (DHW Tank Temperature sensor) now reflects the tank reading.
    assert result["sensors"].th_temp == 46.0
    # Water Inlet (twin_temp) stays honest — never the tank value.
    assert result["sensors"].twin_temp != 46.0


def test_coordinator_uses_default_scan_interval(hass: HomeAssistant):
    """The coordinator always polls at DEFAULT_SCAN_INTERVAL (60s) -- fixed,
    not user-configurable (there is no options flow).
    """
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord = ILetComfortCoordinator(hass, entry)

    assert coord.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)


async def test_poll_fetches_daily_schedule_and_forwards_sn8(hass: HomeAssistant):
    """A poll fetches the daily schedule and threads sn8 to it, like status/sensors."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    coord.appliance_meta = {"sn8": "17186T3A"}
    client = mock_cls.return_value
    client.query_status.return_value = ITSStatus(mode=0)
    client.query_sensors.return_value = ITSSensors()
    schedule = [object(), object(), object(), object()]
    client.query_daily_schedule.return_value = schedule

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    client.query_daily_schedule.assert_called_once_with("APPL1", "17186T3A")
    assert result["schedule"] is schedule


async def test_poll_skips_schedule_refetch_within_config_fetch_interval(
    hass: HomeAssistant,
):
    """A second poll within CONFIG_FETCH_INTERVAL of the last schedule fetch
    reuses the cached schedule instead of hitting the cloud again -- but
    every other query (status/sensors/disinfection/heating element/force
    disinfection/consumption) still runs on the normal 60s cadence.
    """
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    client.query_status.return_value = ITSStatus(mode=0)
    client.query_sensors.return_value = ITSSensors()
    cached_schedule = [object()]
    coord.data = {"status": ITSStatus(), "sensors": ITSSensors(), "schedule": cached_schedule}
    # Simulate a schedule fetch that "just happened" a moment ago.
    coord._last_config_fetch = dt_util.utcnow()

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    client.query_daily_schedule.assert_not_called()
    assert result["schedule"] is cached_schedule
    client.query_disinfection.assert_called_once()
    client.query_heating_element.assert_called_once()
    client.query_force_disinfection.assert_called_once()
    client.query_consumption.assert_called_once()


async def test_poll_refetches_schedule_after_config_fetch_interval_elapses(
    hass: HomeAssistant,
):
    """Once CONFIG_FETCH_INTERVAL has passed since the last schedule fetch,
    the next poll fetches it again.
    """
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    client.query_status.return_value = ITSStatus(mode=0)
    client.query_sensors.return_value = ITSSensors()
    fresh_schedule = [object()]
    client.query_daily_schedule.return_value = fresh_schedule
    coord.data = {"status": ITSStatus(), "sensors": ITSSensors(), "schedule": [object()]}
    coord._last_config_fetch = dt_util.utcnow() - CONFIG_FETCH_INTERVAL - timedelta(seconds=1)

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    client.query_daily_schedule.assert_called_once()
    assert result["schedule"] is fresh_schedule


async def test_poll_fetches_disinfection_and_forwards_sn8(hass: HomeAssistant):
    """A poll fetches disinfection settings and threads sn8 to it, alongside schedule."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    coord.appliance_meta = {"sn8": "17186T3A"}
    client = mock_cls.return_value
    client.query_status.return_value = ITSStatus(mode=0)
    client.query_sensors.return_value = ITSSensors()
    disinfection = object()
    client.query_disinfection.return_value = disinfection

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    client.query_disinfection.assert_called_once_with("APPL1", "17186T3A")
    assert result["disinfection"] is disinfection


async def test_poll_disinfection_failure_falls_back_to_cache(hass: HomeAssistant):
    """A disinfection-fetch failure keeps the cached settings, not None.

    This is bonus config data (not core status/sensors), so it degrades
    quietly to cache rather than raising or blanking the Disinfection switch.
    """
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    client.query_status.return_value = ITSStatus(mode=0)
    client.query_sensors.return_value = ITSSensors()
    cached_disinfection = object()
    coord.data = {
        "status": ITSStatus(), "sensors": ITSSensors(), "schedule": [],
        "disinfection": cached_disinfection,
    }
    client.query_disinfection.side_effect = ApiError("code=1214, msg=System error")

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    assert result["disinfection"] is cached_disinfection
    assert result["status"].mode == 0


async def test_poll_fetches_heating_element_and_forwards_sn8(hass: HomeAssistant):
    """A poll fetches the heating element state and threads sn8 to it."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    coord.appliance_meta = {"sn8": "17186T3A"}
    client = mock_cls.return_value
    client.query_status.return_value = ITSStatus(mode=0)
    client.query_sensors.return_value = ITSSensors()
    client.query_heating_element.return_value = True

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    client.query_heating_element.assert_called_once_with("APPL1", "17186T3A")
    assert result["heating_element"] is True


async def test_poll_heating_element_failure_falls_back_to_cache(hass: HomeAssistant):
    """A heating-element-fetch failure keeps the cached value, not None.

    This is bonus config data (not core status/sensors), so it degrades
    quietly to cache rather than raising or blanking the switch.
    """
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    client.query_status.return_value = ITSStatus(mode=0)
    client.query_sensors.return_value = ITSSensors()
    coord.data = {
        "status": ITSStatus(), "sensors": ITSSensors(), "schedule": [],
        "heating_element": True,
    }
    client.query_heating_element.side_effect = ApiError("code=1214, msg=System error")

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    assert result["heating_element"] is True
    assert result["status"].mode == 0


async def test_poll_fetches_force_disinfection_and_forwards_sn8(hass: HomeAssistant):
    """A poll fetches Force Disinfection state and threads sn8 to it."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    coord.appliance_meta = {"sn8": "17186T3A"}
    client = mock_cls.return_value
    client.query_status.return_value = ITSStatus(mode=0)
    client.query_sensors.return_value = ITSSensors()
    client.query_force_disinfection.return_value = True

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    client.query_force_disinfection.assert_called_once_with("APPL1", "17186T3A")
    assert result["force_disinfection"] is True


async def test_poll_force_disinfection_failure_falls_back_to_cache(hass: HomeAssistant):
    """A Force-Disinfection-fetch failure keeps the cached value, not None."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    client.query_status.return_value = ITSStatus(mode=0)
    client.query_sensors.return_value = ITSSensors()
    coord.data = {
        "status": ITSStatus(), "sensors": ITSSensors(), "schedule": [],
        "force_disinfection": True,
    }
    client.query_force_disinfection.side_effect = ApiError("code=1214, msg=System error")

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    assert result["force_disinfection"] is True
    assert result["status"].mode == 0


async def test_poll_fetches_consumption_and_forwards_sn8(hass: HomeAssistant):
    """A poll fetches Consumption-page data and threads sn8 to it."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    coord.appliance_meta = {"sn8": "17186T3A"}
    client = mock_cls.return_value
    client.query_status.return_value = ITSStatus(mode=0)
    client.query_sensors.return_value = ITSSensors()
    consumption = object()
    client.query_consumption.return_value = consumption

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    client.query_consumption.assert_called_once_with("APPL1", "17186T3A")
    assert result["consumption"] is consumption


async def test_poll_consumption_failure_falls_back_to_cache(hass: HomeAssistant):
    """A Consumption-fetch failure keeps the cached value, not None."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    client.query_status.return_value = ITSStatus(mode=0)
    client.query_sensors.return_value = ITSSensors()
    cached_consumption = object()
    coord.data = {
        "status": ITSStatus(), "sensors": ITSSensors(), "schedule": [],
        "consumption": cached_consumption,
    }
    client.query_consumption.side_effect = ApiError("code=1214, msg=System error")

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    assert result["consumption"] is cached_consumption
    assert result["status"].mode == 0


async def test_poll_daily_schedule_failure_falls_back_to_cache(hass: HomeAssistant):
    """A schedule-fetch failure keeps the cached schedule, not an empty one.

    This is bonus config data (not core status/sensors), so it degrades
    quietly to cache rather than raising or blanking the schedule entities.
    """
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    client.query_status.return_value = ITSStatus(mode=0)
    client.query_sensors.return_value = ITSSensors()
    cached_schedule = [object()]
    coord.data = {
        "status": ITSStatus(), "sensors": ITSSensors(), "schedule": cached_schedule,
    }
    client.query_daily_schedule.side_effect = ApiError("code=1214, msg=System error")

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    assert result["schedule"] is cached_schedule
    # Status/sensors are unaffected by the schedule fetch failing.
    assert result["status"].mode == 0


async def test_poll_standard_leaves_sensors_untouched(hass: HomeAssistant):
    """With no sn8 the poll resolves STANDARD and never rewrites the sensors."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    client.query_status.return_value = ITSStatus(box_bottom_temp=99.0, mode=1)
    sensors = ITSSensors(twin_temp=12.0)
    client.query_sensors.return_value = sensors

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    assert client.query_status.call_args.args == ("APPL1", None)
    assert client.query_sensors.call_args.args == ("APPL1", None)
    assert result["sensors"] is sensors  # STANDARD is a no-op (object identity)
    assert result["sensors"].twin_temp == 12.0  # unchanged by STANDARD


async def test_async_set_device_threads_sn8_to_client(hass: HomeAssistant):
    """The SET path must forward the appliance sn8 so the client can branch the
    write encoding per model (KJRH-120L short commands vs the legacy C3 frame)."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    coord.appliance_meta = {"sn8": "17100003"}
    client = mock_cls.return_value
    coord.async_request_refresh = AsyncMock()

    await coord.async_set_device(temperature=60)

    assert client.set_device.call_args.args == ("APPL1",)
    assert client.set_device.call_args.kwargs["sn8"] == "17100003"
    assert client.set_device.call_args.kwargs["temperature"] == 60


async def test_async_set_disinfection_forwards_to_client(hass: HomeAssistant):
    """The Disinfection switch's write path forwards straight to the client."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    coord.async_request_refresh = AsyncMock()

    await coord.async_set_disinfection(
        enabled=False, hour=14, minute=0, temp_c=65.0, cycle_days=7,
    )

    assert client.set_disinfection.call_args.args == ("APPL1",)
    assert client.set_disinfection.call_args.kwargs == {
        "enabled": False, "hour": 14, "minute": 0, "temp_c": 65.0, "cycle_days": 7,
    }
    coord.async_request_refresh.assert_awaited_once()


async def test_async_set_heating_element_forwards_to_client(hass: HomeAssistant):
    """The Heating Element switch's write path forwards straight to the client."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    coord.async_request_refresh = AsyncMock()

    await coord.async_set_heating_element(enabled=True)

    assert client.set_heating_element.call_args.args == ("APPL1",)
    assert client.set_heating_element.call_args.kwargs == {"enabled": True}
    coord.async_request_refresh.assert_awaited_once()


async def test_async_set_force_disinfection_forwards_to_client(hass: HomeAssistant):
    """The Force Disinfection switch's write path forwards straight to the client."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    coord.async_request_refresh = AsyncMock()

    await coord.async_set_force_disinfection(enabled=True)

    assert client.set_force_disinfection.call_args.args == ("APPL1",)
    assert client.set_force_disinfection.call_args.kwargs == {"enabled": True}
    coord.async_request_refresh.assert_awaited_once()


async def test_async_set_schedule_active_forwards_to_client_and_forces_refetch(
    hass: HomeAssistant,
):
    """The Daily Schedule Active switch's write path forwards straight to
    the client, and clears _last_config_fetch so the next poll refetches
    the schedule immediately instead of waiting out CONFIG_FETCH_INTERVAL.
    """
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    coord.async_request_refresh = AsyncMock()
    coord._last_config_fetch = dt_util.utcnow()

    await coord.async_set_schedule_active(slot=2, enabled=True)

    assert client.set_schedule_active.call_args.args == ("APPL1",)
    assert client.set_schedule_active.call_args.kwargs == {"slot": 2, "enabled": True}
    assert coord._last_config_fetch is None
    coord.async_request_refresh.assert_awaited_once()


def _degraded_coordinator(hass: HomeAssistant) -> tuple[ILetComfortCoordinator, MagicMock]:
    """Build a coordinator wired so both queries fall back to cache."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)
    client = mock_cls.return_value
    coord.data = {"status": ITSStatus(mode=1), "sensors": ITSSensors()}
    return coord, client


def _issue_id(coord: ILetComfortCoordinator) -> str:
    return OFFLINE_REPAIR_ID.format(entry_id=coord.entry.entry_id)


async def test_offline_repair_card_created_after_threshold(hass: HomeAssistant):
    """After OFFLINE_REPAIR_THRESHOLD consecutive both-degraded polls, a Repair appears."""
    coord, client = _degraded_coordinator(hass)
    client.query_status.side_effect = ApiError("truncated frame")
    client.query_sensors.side_effect = ApiError("truncated frame")

    registry = ir.async_get(hass)
    issue_id = _issue_id(coord)

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        for _ in range(OFFLINE_REPAIR_THRESHOLD - 1):
            await coord._poll()
            assert registry.async_get_issue(DOMAIN, issue_id) is None

        await coord._poll()
        issue = registry.async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.severity == ir.IssueSeverity.WARNING
        assert issue.translation_key == "device_offline"


async def test_offline_repair_card_masks_appliance_code_placeholder(
    hass: HomeAssistant,
):
    """The offline Repair card must show a suffix-masked appliance_code, not the
    full device-unique id (it surfaces in shareable screenshots/diagnostics)."""
    coord, client = _degraded_coordinator(hass)
    coord.appliance_code = "153931629126443"
    client.query_status.side_effect = ApiError("truncated frame")
    client.query_sensors.side_effect = ApiError("truncated frame")

    registry = ir.async_get(hass)
    issue_id = _issue_id(coord)

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        for _ in range(OFFLINE_REPAIR_THRESHOLD):
            await coord._poll()

    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders == {"appliance_code": "15393…"}


async def test_offline_repair_card_not_created_when_only_one_query_fails(
    hass: HomeAssistant,
):
    """Sensors-only failure (or status-only) must not surface the offline Repair."""
    coord, client = _degraded_coordinator(hass)
    client.query_status.return_value = ITSStatus(mode=1)
    client.query_sensors.side_effect = ApiError("truncated frame")

    registry = ir.async_get(hass)
    issue_id = _issue_id(coord)

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        for _ in range(OFFLINE_REPAIR_THRESHOLD + 2):
            await coord._poll()

    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_offline_repair_card_cleared_on_recovery(hass: HomeAssistant):
    """A single healthy poll clears the Repair card."""
    coord, client = _degraded_coordinator(hass)
    client.query_status.side_effect = ApiError("truncated frame")
    client.query_sensors.side_effect = ApiError("truncated frame")

    registry = ir.async_get(hass)
    issue_id = _issue_id(coord)

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        for _ in range(OFFLINE_REPAIR_THRESHOLD):
            await coord._poll()
        assert registry.async_get_issue(DOMAIN, issue_id) is not None

        client.query_status.side_effect = None
        client.query_status.return_value = ITSStatus(mode=1)
        client.query_sensors.side_effect = None
        client.query_sensors.return_value = ITSSensors()
        await coord._poll()

    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_offline_repair_card_reraised_after_recovery_then_redegradation(
    hass: HomeAssistant,
):
    """After clear → degraded again, the Repair card must reappear on threshold."""
    coord, client = _degraded_coordinator(hass)
    client.query_status.side_effect = ApiError("truncated frame")
    client.query_sensors.side_effect = ApiError("truncated frame")

    registry = ir.async_get(hass)
    issue_id = _issue_id(coord)

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        for _ in range(OFFLINE_REPAIR_THRESHOLD):
            await coord._poll()
        assert registry.async_get_issue(DOMAIN, issue_id) is not None

        # Recover.
        client.query_status.side_effect = None
        client.query_status.return_value = ITSStatus(mode=1)
        client.query_sensors.side_effect = None
        client.query_sensors.return_value = ITSSensors()
        await coord._poll()
        assert registry.async_get_issue(DOMAIN, issue_id) is None

        # Degrade again.
        client.query_status.side_effect = ApiError("truncated frame")
        client.query_sensors.side_effect = ApiError("truncated frame")
        for _ in range(OFFLINE_REPAIR_THRESHOLD):
            await coord._poll()

    assert registry.async_get_issue(DOMAIN, issue_id) is not None
