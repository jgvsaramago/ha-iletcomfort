"""Switch entities for the iLetComfort integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import build_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ILetComfortBoostSwitch(coordinator),
        ILetComfortSilenceSwitch(coordinator),
        ILetComfortDisinfectionSwitch(coordinator),
        ILetComfortHeatingElementSwitch(coordinator),
    ])


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

    Named "Disinfection Routine" (not just "Disinfection") since a future
    "Manual Disinfection" switch will trigger a one-off cycle on the same
    device — this one is the scheduled routine (enable + hour/minute/
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
