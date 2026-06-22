"""Number entities for the iLetComfort integration.

Currently this platform exposes a single, EXPERIMENTAL entity: the DHW setpoint
control for the KJRH-120L *dual* heating variant (sn8 17100003, issue #5).

That sn8 fronts two unit types. The pure-DHW water heater (reporter phillip)
keeps a single climate entity = DHW setpoint and gets NO number entity here. The
dual heating unit (reporter minoo221) exposes BOTH a Room/Zone-1 setpoint (the
climate entity) AND a DHW setpoint — this number entity surfaces the latter so
the two are no longer conflated. The entity is only created when the unit's
status frame reports the dual capability bytes (``kjrh120l_has_zone1``).
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import build_device_info
from .model_profiles import (
    KJRH120L_DHW_TEMP_MAX,
    KJRH120L_DHW_TEMP_MIN,
    ModelProfile,
    kjrh120l_has_zone1,
    resolve_profile,
)


def _is_kjrh120l_dual(coordinator: ILetComfortCoordinator) -> bool:
    """True when this appliance is the EXPERIMENTAL KJRH-120L dual variant.

    Gated strictly: the sn8 must resolve to the KJRH120L profile AND the current
    status frame must carry the dual capability bytes (body[8]==1 and body[9]==1).
    """
    if resolve_profile(coordinator.sn8) is not ModelProfile.KJRH120L:
        return False
    status = (coordinator.data or {}).get("status")
    if status is None or not status.raw_body:
        return False
    return kjrh120l_has_zone1(status.raw_body)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities.

    Only the KJRH-120L dual heating variant gets a DHW setpoint number; every
    other appliance (including the pure-DHW KJRH-120L) gets no number entity.
    """
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    if _is_kjrh120l_dual(coordinator):
        async_add_entities([ILetComfortKjrh120lDhwSetpoint(coordinator)])


class ILetComfortKjrh120lDhwSetpoint(
    CoordinatorEntity[ILetComfortCoordinator], NumberEntity
):
    """EXPERIMENTAL DHW setpoint control for the KJRH-120L dual variant (#5).

    Reads the DHW setpoint decoded from status body[15]
    (``status.kjrh120l_dhw_setpoint``) and writes it with the confirmed DHW
    command ``0007 01 <temp> ff``. The Room/Zone-1 setpoint is the climate entity.
    """

    _attr_has_entity_name = True
    _attr_name = "DHW Setpoint"
    _attr_icon = "mdi:water-thermometer"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = float(KJRH120L_DHW_TEMP_MIN)
    _attr_native_max_value = float(KJRH120L_DHW_TEMP_MAX)
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_kjrh120l_dhw_setpoint"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        status = self.coordinator.data.get("status")
        if status is None:
            return None
        return status.kjrh120l_dhw_setpoint

    async def async_set_native_value(self, value: float) -> None:
        # Clamp to the DHW range before sending the confirmed field-0x07 write.
        clamped = max(
            self._attr_native_min_value,
            min(float(value), self._attr_native_max_value),
        )
        await self.coordinator.async_set_device(temperature=int(clamped))
