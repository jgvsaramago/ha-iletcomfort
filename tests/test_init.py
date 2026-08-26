"""Tests for the iLetComfort migration logic."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort import (
    _log_entity_registry_collisions,
    _remove_stale_daily_schedule_binary_sensors,
    async_migrate_entry,
)
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DEFAULT_REGION,
    DOMAIN,
    REGION_EU,
    REGION_US,
)


async def test_migrate_v1_sets_new_unique_id_and_region(hass: HomeAssistant):
    """v1 entries (unique_id=email) must be migrated to v2 (email:code, region=us)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",  # v1 format: bare email
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
            # NOTE: no CONF_REGION — v1 entries didn't have it
        },
        version=1,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.version == 2
    assert entry.unique_id == "user@example.com:APPL1"
    assert entry.data[CONF_REGION] == DEFAULT_REGION


async def test_migrate_v1_preserves_existing_region(hass: HomeAssistant):
    """If a v1 entry happens to have CONF_REGION already, don't overwrite it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_EU,
        },
        version=1,
    )
    entry.add_to_hass(hass)

    await async_migrate_entry(hass, entry)

    assert entry.version == 2
    assert entry.data[CONF_REGION] == REGION_EU


async def test_migrate_v1_lowercases_email_in_unique_id(hass: HomeAssistant):
    """The migrated unique_id must use a lowercased email so dedup works."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="USER@Example.com",
        data={
            CONF_EMAIL: "USER@Example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
        },
        version=1,
    )
    entry.add_to_hass(hass)

    await async_migrate_entry(hass, entry)

    assert entry.unique_id == "user@example.com:APPL1"


async def test_migrate_v1_renames_shared_token_file_to_per_entry_path(
    hass: HomeAssistant,
):
    """The old shared token file must be renamed to the per-entry path.

    Before this fix, bumping the token filename to include entry_id left the
    old `.storage/iletcomfort_token` orphaned and forced a re-login.
    """
    storage = Path(hass.config.path(".storage"))
    storage.mkdir(parents=True, exist_ok=True)
    old_path = storage / "iletcomfort_token"
    old_path.write_text('{"access_token": "legacy"}', encoding="utf-8")

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
        },
        version=1,
    )
    entry.add_to_hass(hass)

    await async_migrate_entry(hass, entry)

    new_path = storage / f"iletcomfort_token_{entry.entry_id}"
    assert new_path.exists()
    assert new_path.read_text(encoding="utf-8") == '{"access_token": "legacy"}'
    assert not old_path.exists()


async def test_migrate_v1_handles_missing_token_file(hass: HomeAssistant):
    """Migration must not fail if no old token file exists (fresh installs)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
        },
        version=1,
    )
    entry.add_to_hass(hass)

    # Should not raise.
    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2


async def test_migrate_v2_is_noop(hass: HomeAssistant):
    """A v2 entry must pass through migration untouched."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com:APPL1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_REGION: REGION_EU,
            CONF_APPLIANCE_CODE: "APPL1",
        },
        version=2,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2
    assert entry.unique_id == "user@example.com:APPL1"
    assert entry.data[CONF_REGION] == REGION_EU


async def test_log_entity_registry_collisions_logs_every_domain_row(
    hass: HomeAssistant, caplog,
):
    """Diagnostic for "Error adding entity ... with platform iletcomfort":
    every registry row for this domain must be logged, including one tied to
    a DIFFERENT (e.g. stale/removed) config entry -- that's exactly the kind
    of leftover that causes the collision, so it must not be filtered out by
    only looking at the entry currently being set up.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com:APPL1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_REGION: REGION_US,
            CONF_APPLIANCE_CODE: "APPL1",
        },
        version=2,
    )
    entry.add_to_hass(hass)

    other_entry = MockConfigEntry(domain=DOMAIN, unique_id="stale:APPL1")
    other_entry.add_to_hass(hass)

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", DOMAIN, "APPL1_daily_schedule_4_end_time",
        config_entry=entry,
    )
    registry.async_get_or_create(
        "sensor", DOMAIN, "STALE_daily_schedule_4_end_time",
        config_entry=other_entry,
    )
    # A foreign-domain row must not be logged as if it were ours.
    registry.async_get_or_create("sensor", "other_integration", "unrelated")

    with caplog.at_level(logging.DEBUG, logger="custom_components.iletcomfort"):
        _log_entity_registry_collisions(hass, entry)

    logged = "\n".join(
        r.getMessage() for r in caplog.records
        if r.name == "custom_components.iletcomfort"
    )
    assert "APPL1_daily_schedule_4_end_time" in logged
    assert "STALE_daily_schedule_4_end_time" in logged
    assert "unrelated" not in logged


async def test_remove_stale_daily_schedule_binary_sensors_removes_all_four_slots(
    hass: HomeAssistant,
):
    """"Daily Schedule N Active" moved from binary_sensor to switch (see
    switch.py). Any leftover binary_sensor registry row from before that
    move must be removed, not left "unavailable" forever -- exactly the
    class of bug the old options-flow fetch toggles caused.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com:APPL1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_REGION: REGION_US,
            CONF_APPLIANCE_CODE: "APPL1",
        },
        version=2,
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    stale_ids = []
    for slot in range(1, 5):
        entry_reg = registry.async_get_or_create(
            "binary_sensor", DOMAIN, f"APPL1_daily_schedule_{slot}_active",
            config_entry=entry,
        )
        stale_ids.append(entry_reg.entity_id)
    # A same-named switch entity (the replacement) must survive untouched.
    live_switch = registry.async_get_or_create(
        "switch", DOMAIN, "APPL1_daily_schedule_1_active", config_entry=entry,
    )
    # An unrelated binary_sensor must survive untouched too.
    unrelated = registry.async_get_or_create(
        "binary_sensor", DOMAIN, "APPL1_compressor_running", config_entry=entry,
    )

    _remove_stale_daily_schedule_binary_sensors(hass, entry)

    for entity_id in stale_ids:
        assert registry.async_get(entity_id) is None
    assert registry.async_get(live_switch.entity_id) is not None
    assert registry.async_get(unrelated.entity_id) is not None

    # Idempotent: running it again (e.g. next reload) must not raise.
    _remove_stale_daily_schedule_binary_sensors(hass, entry)
