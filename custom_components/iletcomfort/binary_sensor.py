"""Binary sensor entities for the iLetComfort integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import build_device_info


@dataclass(frozen=True, kw_only=True)
class ILetComfortBinarySensorDescription(BinarySensorEntityDescription):
    """Describe an iLetComfort binary sensor."""

    is_on_fn: Any  # Callable[[dict], bool]


BINARY_SENSOR_DESCRIPTIONS: tuple[ILetComfortBinarySensorDescription, ...] = (
    ILetComfortBinarySensorDescription(
        key="compressor_running",
        name="Compressor Running",
        device_class=BinarySensorDeviceClass.RUNNING,
        is_on_fn=lambda data: (
            data.get("status") is not None and data["status"].comp_running
        ),
    ),
    ILetComfortBinarySensorDescription(
        key="ibh_running",
        name="IBH Running",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda data: (
            data.get("status") is not None and data["status"].ibh_running
        ),
    ),
    ILetComfortBinarySensorDescription(
        key="error",
        name="Error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda data: (
            data.get("status") is not None and data["status"].error_code != 0
        ),
    ),
    # "Daily Schedule N Active" used to live here as a read-only binary_sensor.
    # Now that activating/deactivating a slot is a confirmed write (see
    # switch.py's ILetComfortDailyScheduleActiveSwitch), it's a switch
    # instead — CONFIG is the correct category for something the user can
    # change, which a binary_sensor can never be (see the sensor.py note on
    # why the OTHER daily-schedule fields stay DIAGNOSTIC). See
    # __init__.py's _remove_stale_daily_schedule_binary_sensors for the
    # matching entity-registry cleanup so the old entities don't linger
    # "unavailable".
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor entities."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ILetComfortBinarySensor(coordinator, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )


class ILetComfortBinarySensor(
    CoordinatorEntity[ILetComfortCoordinator], BinarySensorEntity,
):
    """Binary sensor entity for iLetComfort heat pump."""

    _attr_has_entity_name = True
    entity_description: ILetComfortBinarySensorDescription

    def __init__(
        self,
        coordinator: ILetComfortCoordinator,
        description: ILetComfortBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.appliance_code}_{description.key}"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def is_on(self) -> bool:
        if self.coordinator.data is None:
            return False
        return self.entity_description.is_on_fn(self.coordinator.data)
