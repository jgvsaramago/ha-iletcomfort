"""Climate entity for the iLetComfort integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import (
    MODE_COOL,
    MODE_HEAT,
    MODE_OFF,
    MODE_WATERPUMP,
    QUERY_TO_SET_MODE,
    TEMP_RANGES,
)
from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import build_device_info
from .model_profiles import (
    KJRH120L_ROOM_TEMP_MAX,
    KJRH120L_ROOM_TEMP_MIN,
    KJRH120L_TEMP_MAX,
    KJRH120L_TEMP_MIN,
    ModelProfile,
    kjrh120l_has_zone1,
    resolve_profile,
)

# Query response mode (from device) → HA HVAC mode
_QUERY_MODE_TO_HVAC: dict[int, HVACMode] = {
    0: HVACMode.OFF,
    1: HVACMode.HEAT,
    2: HVACMode.COOL,
    4: HVACMode.FAN_ONLY,  # water pump / circulation
}

# HA HVAC mode → SET command mode
_HVAC_TO_SET_MODE: dict[HVACMode, int] = {
    HVACMode.OFF: MODE_OFF,
    HVACMode.HEAT: MODE_HEAT,
    HVACMode.COOL: MODE_COOL,
    HVACMode.FAN_ONLY: MODE_WATERPUMP,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the climate entity."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ILetComfortClimate(coordinator)])


class ILetComfortClimate(CoordinatorEntity[ILetComfortCoordinator], ClimateEntity):
    """Climate entity for ITS heat pump."""

    _attr_has_entity_name = True
    _attr_name = "Heat Pump"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.FAN_ONLY]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_target_temperature_step = 1.0

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_climate"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def _status(self):
        if self.coordinator.data:
            return self.coordinator.data.get("status")
        return None

    @property
    def _sensors(self):
        if self.coordinator.data:
            return self.coordinator.data.get("sensors")
        return None

    @property
    def hvac_mode(self) -> HVACMode:
        if self._status is None:
            return HVACMode.OFF
        return _QUERY_MODE_TO_HVAC.get(self._status.mode, HVACMode.OFF)

    @property
    def _profile(self) -> ModelProfile:
        """Resolve the decode profile from the coordinator's sn8 model code."""
        return resolve_profile(self.coordinator.sn8)

    @property
    def _is_kjrh120l_dual(self) -> bool:
        """True for the EXPERIMENTAL KJRH-120L dual heating variant (issue #5).

        The dual unit (reporter minoo221) exposes BOTH a Room/Zone-1 setpoint and
        a DHW setpoint; this climate entity then tracks Room, and a separate DHW
        number entity tracks the tank. False for the pure-DHW KJRH-120L (reporter
        phillip) and every other model — the climate entity is unchanged there.
        Gated strictly on the status frame's capability bytes.
        """
        if self._profile is not ModelProfile.KJRH120L:
            return False
        if self._status is None or not self._status.raw_body:
            return False
        return kjrh120l_has_zone1(self._status.raw_body)

    @property
    def hvac_modes(self) -> list[HVACMode]:
        # The KJRH-120L dual heating variant supports HEAT / OFF only: the unit
        # delays/blocks cool valve-switching for dew-point protection and we have
        # no confirmed cool command, so COOL/FAN_ONLY are excluded here (issue #5).
        if self._is_kjrh120l_dual:
            return [HVACMode.OFF, HVACMode.HEAT]
        return self._attr_hvac_modes

    @property
    def current_temperature(self) -> float | None:
        if self._sensors is None:
            return None
        # Profile-aware: ATW/AQUAPURA have no real water-inlet reading, so the
        # meaningful "current" value is the DHW tank temp the profiles surface on
        # th_temp. STANDARD is unchanged: it reads the real inlet (twin_temp).
        if self._profile in (ModelProfile.ATW, ModelProfile.AQUAPURA):
            return self._sensors.th_temp
        return self._sensors.twin_temp

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = {}
        if self._sensors is not None:
            if self._sensors.twin_temp is not None:
                attrs["water_inlet"] = self._sensors.twin_temp
            if self._sensors.twout_temp is not None:
                attrs["water_outlet"] = self._sensors.twout_temp
            if self._sensors.t4_temp is not None:
                attrs["outdoor_ambient"] = self._sensors.t4_temp
        return attrs

    @property
    def target_temperature(self) -> float | None:
        if self._status is None:
            return None
        # t5s_def (d+2, offset-encoded) is the active mode setpoint.
        # set_temperature (d+4, direct) is the DHW tank target.
        if self._status.t5s_def is not None:
            return self._status.t5s_def
        return float(self._status.set_temperature)



    @property
    def min_temp(self) -> float:
        # KJRH-120L dual variant: this climate tracks the Room/Zone-1 setpoint,
        # so use the air-side comfort range (issue #5).
        if self._is_kjrh120l_dual:
            return float(KJRH120L_ROOM_TEMP_MIN)
        # Pure-DHW KJRH-120L is a heat-pump water heater; its setpoint range sits
        # above the air-side HEAT range (issue #35).
        if self._profile is ModelProfile.KJRH120L:
            return float(KJRH120L_TEMP_MIN)
        set_mode = _HVAC_TO_SET_MODE.get(self.hvac_mode)
        if set_mode is not None and set_mode in TEMP_RANGES:
            return float(TEMP_RANGES[set_mode][0])
        return 10.0

    @property
    def max_temp(self) -> float:
        if self._is_kjrh120l_dual:
            return float(KJRH120L_ROOM_TEMP_MAX)
        if self._profile is ModelProfile.KJRH120L:
            return float(KJRH120L_TEMP_MAX)
        set_mode = _HVAC_TO_SET_MODE.get(self.hvac_mode)
        if set_mode is not None and set_mode in TEMP_RANGES:
            return float(TEMP_RANGES[set_mode][1])
        return 40.0

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        set_mode = _HVAC_TO_SET_MODE.get(hvac_mode)
        if set_mode is not None:
            await self.coordinator.async_set_device(mode=set_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None:
            # Clamp to the entity's min/max before sending. For the KJRH-120L
            # this keeps the short setpoint write inside the valid range; for
            # other profiles the value is already mode-constrained by HA.
            clamped = max(self.min_temp, min(float(temp), self.max_temp))
            if self._is_kjrh120l_dual:
                # Dual variant: the climate entity is the Room/Zone-1 setpoint,
                # written via the EXPERIMENTAL field-0x08 command (issue #5). The
                # DHW setpoint is a separate number entity.
                await self.coordinator.async_set_device(
                    room_temperature=int(clamped)
                )
            else:
                await self.coordinator.async_set_device(temperature=int(clamped))

    async def async_turn_on(self) -> None:
        await self.coordinator.async_set_device(power_on=True)

    async def async_turn_off(self) -> None:
        await self.coordinator.async_set_device(mode=MODE_OFF)
