"""Switch entities for the iLetComfort integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import build_device_info
from .model_profiles import (
    AQUAPURA_SPLIT_GREEN_SCHEDULE_SLOT_COUNT,
    BOOST_UNSUPPORTED_PROFILES,
    resolve_profile,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = []
    if resolve_profile(coordinator.sn8) not in BOOST_UNSUPPORTED_PROFILES:
        # Don't register a control that would sit there doing nothing when
        # pressed — see BOOST_UNSUPPORTED_PROFILES's docstring.
        entities.append(ILetComfortBoostSwitch(coordinator))
    entities += [
        ILetComfortSilenceSwitch(coordinator),
        ILetComfortDisinfectionSwitch(coordinator),
        ILetComfortHeatingElementSwitch(coordinator),
        ILetComfortForceDisinfectionSwitch(coordinator),
        *(
            ILetComfortDailyScheduleActiveSwitch(coordinator, n)
            for n in range(1, AQUAPURA_SPLIT_GREEN_SCHEDULE_SLOT_COUNT + 1)
        ),
    ]
    async_add_entities(entities)


class ILetComfortBoostSwitch(CoordinatorEntity[ILetComfortCoordinator], SwitchEntity):
    """Switch entity for boost mode."""

    _attr_has_entity_name = True
    _attr_name = "Boost"
    _attr_icon = "mdi:rocket-launch"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_boost"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def is_on(self) -> bool:
        if self.coordinator.data is None:
            return False
        sensors = self.coordinator.data.get("sensors")
        if sensors is None:
            return False
        return sensors.ctrl_flag == 2

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_device(boost=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_device(boost=False)


class ILetComfortSilenceSwitch(CoordinatorEntity[ILetComfortCoordinator], SwitchEntity):
    """Switch entity for the Aquapura Split Green's "Silence" quiet mode.

    Only that profile's status ever populates ``status.silence``; every other
    profile leaves it None, so this reads "off" for them.
    """

    _attr_has_entity_name = True
    _attr_name = "Silence"
    _attr_icon = "mdi:volume-mute"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_silence"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def is_on(self) -> bool:
        if self.coordinator.data is None:
            return False
        status = self.coordinator.data.get("status")
        if status is None:
            return False
        return bool(status.silence)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_device(silence=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_device(silence=False)


class ILetComfortDisinfectionSwitch(
    CoordinatorEntity[ILetComfortCoordinator], SwitchEntity,
):
    """Switch entity for the Aquapura Split Green's "Desinfecção" (disinfection) mode.

    Named "Disinfection Routine" (not just "Disinfection") since
    ``ILetComfortForceDisinfectionSwitch`` triggers a one-off cycle on the
    same device — this one is the scheduled routine (enable + hour/minute/
    temperature/cycle-days), matching what the app's Desinfecção submenu
    shows. Declared unconditionally like the other switches; every other
    profile never populates ``coordinator.data["disinfection"]``, so this
    reads "off" and its writes raise (see ``_async_set``) for them. The
    hour/minute/temperature/cycle-days themselves are surfaced as separate
    Diagnostic sensors (sensor.py's ``disinfection_*`` descriptions), not
    duplicated here as attributes. Toggling this resends those current
    values along with the new enable bit — the device's write command always
    carries all five fields together (see
    ``build_aquapura_split_green_disinfection_command``).
    """

    _attr_has_entity_name = True
    _attr_name = "Disinfection Routine"
    _attr_icon = "mdi:bacteria-outline"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_disinfection"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def _settings(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("disinfection")

    @property
    def is_on(self) -> bool:
        settings = self._settings
        return bool(settings is not None and settings.enabled)

    async def _async_set(self, enabled: bool) -> None:
        settings = self._settings
        if settings is None:
            raise HomeAssistantError(
                "Disinfection settings are not available yet; try again after "
                "the next poll."
            )
        await self.coordinator.async_set_disinfection(
            enabled=enabled,
            hour=settings.hour,
            minute=settings.minute,
            temp_c=settings.temperature,
            cycle_days=settings.cycle_days,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)


class ILetComfortHeatingElementSwitch(
    CoordinatorEntity[ILetComfortCoordinator], SwitchEntity,
):
    """Switch entity for the Aquapura Split Green's electric heating element.

    Lives in the TIMERS (01,90) frame — a separate selector from every other
    single-field write (all on STATUS, 01,f4). Only that profile's coordinator
    data ever populates ``coordinator.data["heating_element"]``; every other
    profile leaves it None, so this reads "off" for them.
    """

    _attr_has_entity_name = True
    _attr_name = "Heating Element"
    _attr_icon = "mdi:radiator"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_heating_element"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def is_on(self) -> bool:
        if self.coordinator.data is None:
            return False
        return bool(self.coordinator.data.get("heating_element"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_heating_element(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_heating_element(enabled=False)


class ILetComfortForceDisinfectionSwitch(
    CoordinatorEntity[ILetComfortCoordinator], SwitchEntity,
):
    """Switch entity for the Aquapura Split Green's "Force Disinfection"
    (manual, one-off) cycle — the app's counterpart to the scheduled
    **Disinfection Routine** switch, on the same TIMERS (01,90) frame as the
    heating element (different field/body index). Only that profile's
    coordinator data ever populates ``coordinator.data["force_disinfection"]``;
    every other profile leaves it None, so this reads "off" for them.
    """

    _attr_has_entity_name = True
    _attr_name = "Force Disinfection"
    _attr_icon = "mdi:bacteria"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_force_disinfection"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def is_on(self) -> bool:
        if self.coordinator.data is None:
            return False
        return bool(self.coordinator.data.get("force_disinfection"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_force_disinfection(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_force_disinfection(enabled=False)


class ILetComfortDailyScheduleActiveSwitch(
    CoordinatorEntity[ILetComfortCoordinator], SwitchEntity,
):
    """Switch entity for activating/deactivating Aquapura Split Green daily
    schedule ("Temporiz. N") slot ``slot`` (1-4).

    entity_category=CONFIG is correct here (unlike the OTHER daily-schedule
    fields, which stay DIAGNOSTIC sensors — see sensor.py's note): this is
    the one daily-schedule field the user can actually change, which is
    exactly what CONFIG means. Slots 1-2's write is confirmed byte-exact
    against real captures; slots 3-4's field id is an informed extrapolation
    (see build_aquapura_split_green_schedule_active_command's module notes)
    — if a real capture ever contradicts it, trust the capture. Only that
    profile's coordinator data ever populates ``coordinator.data["schedule"]``;
    every other profile leaves it empty, so this reads "off" for them.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-clock"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ILetComfortCoordinator, slot: int) -> None:
        super().__init__(coordinator)
        self._slot = slot
        self._attr_name = f"Daily Schedule {slot} Active"
        self._attr_unique_id = f"{coordinator.appliance_code}_daily_schedule_{slot}_active"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def is_on(self) -> bool:
        if self.coordinator.data is None:
            return False
        schedule = self.coordinator.data.get("schedule")
        if not schedule or len(schedule) < self._slot:
            return False
        return bool(schedule[self._slot - 1].active)

    async def _async_set(self, enabled: bool) -> None:
        await self.coordinator.async_set_schedule_active(
            slot=self._slot, enabled=enabled,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)
