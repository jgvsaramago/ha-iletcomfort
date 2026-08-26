"""Select entities for the iLetComfort integration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import build_device_info

MUTE_OPTIONS = ["Off", "Level 1", "Level 2"]
_MUTE_TO_API = {"Off": 0, "Level 1": 1, "Level 2": 2}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select entities."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ILetComfortMuteSelect(coordinator),
        ILetComfortAquapuraSplitGreenModeSelect(coordinator),
    ])


class ILetComfortMuteSelect(CoordinatorEntity[ILetComfortCoordinator], SelectEntity):
    """Select entity for mute/silent mode."""

    _attr_has_entity_name = True
    _attr_name = "Silent Mode"
    _attr_icon = "mdi:volume-off"
    _attr_options = MUTE_OPTIONS

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_mute"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def current_option(self) -> str:
        if self.coordinator.data is None:
            return "Off"
        sensors = self.coordinator.data.get("sensors")
        if sensors is None or sensors.ctrl_flag != 1:
            return "Off"
        # mute_level: 0=Level 1, 1=Level 2
        return "Level 2" if sensors.mute_level == 1 else "Level 1"

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_device(mute=_MUTE_TO_API[option])


AQUAPURA_SPLIT_GREEN_MODE_OPTIONS = ["Eco", "Disparo"]


class ILetComfortAquapuraSplitGreenModeSelect(
    CoordinatorEntity[ILetComfortCoordinator], SelectEntity,
):
    """Select entity for the Aquapura Split Green's "Eco"/"Disparo" operating
    preset (sn8 17186T3A; see model_profiles). Registered unconditionally like
    the daily-schedule entities: it reads unknown and silently no-ops on other
    profiles, which don't populate ``status.operating_mode`` or accept the
    ``operating_mode`` write kwarg (the existing Silent Mode select above
    behaves the same way on models that ignore ``mute``).
    """

    _attr_has_entity_name = True
    _attr_name = "DHW Mode"
    _attr_icon = "mdi:leaf"
    _attr_options = AQUAPURA_SPLIT_GREEN_MODE_OPTIONS

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_aquapura_split_green_mode"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def current_option(self) -> str | None:
        if self.coordinator.data is None:
            return None
        status = self.coordinator.data.get("status")
        if status is None:
            return None
        # Only a confirmed label is a valid HA option; an unrecognised marker
        # (reported as "Unknown(0xNN)") or no data at all reads as None/unknown
        # rather than being forced into one of the two known options.
        if status.operating_mode not in AQUAPURA_SPLIT_GREEN_MODE_OPTIONS:
            return None
        return status.operating_mode

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_device(operating_mode=option)
