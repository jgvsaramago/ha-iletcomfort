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
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import build_device_info
from .model_profiles import AQUAPURA_SPLIT_GREEN_SCHEDULE_SLOT_COUNT


@dataclass(frozen=True, kw_only=True)
class ILetComfortBinarySensorDescription(BinarySensorEntityDescription):
    """Describe an iLetComfort binary sensor."""

    is_on_fn: Any  # Callable[[dict], bool]


def _schedule_slot_active(index: int) -> Any:
    """Helper: is Aquapura Split Green daily-schedule slot ``index`` (0-based)
    active. False if there's no schedule data for this device/poll or the
    slot index is out of range (e.g. STANDARD-profile devices).
    """
    def _is_on(data: dict[str, Any]) -> bool:
        schedule = data.get("schedule")
        if not schedule or len(schedule) <= index:
            return False
        return bool(schedule[index].active)
    return _is_on


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
        is_on_fn=lambda data: (
            data.get("status") is not None and data["status"].ibh_running
        ),
    ),
    ILetComfortBinarySensorDescription(
        key="error",
        name="Error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda data: (
            data.get("status") is not None and data["status"].error_code != 0
        ),
    ),
    *(
        ILetComfortBinarySensorDescription(
            key=f"daily_schedule_{n}_active",
            name=f"Daily Schedule {n} Active",
            is_on_fn=_schedule_slot_active(n - 1),
        )
        for n in range(1, AQUAPURA_SPLIT_GREEN_SCHEDULE_SLOT_COUNT + 1)
    ),
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
