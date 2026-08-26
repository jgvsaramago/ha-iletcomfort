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
    AQUAPURA_SPLIT_GREEN_TEMP_MAX,
    AQUAPURA_SPLIT_GREEN_TEMP_MIN,
    KJRH120L_TEMP_MAX,
    KJRH120L_TEMP_MIN,
    ModelProfile,
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

# Aquapura Split Green only: the "Eco"/"Disparo" operating preset, surfaced as
# a climate preset (alongside the Off/Heat hvac_mode already on the same
# card) rather than a separate select entity. Declared unconditionally like
# hvac_modes/supported_features (this is one entity class serving every
# profile); other profiles never populate status.operating_mode, so
# preset_mode just reads None for them (see PRESET_MODES constant below).
AQUAPURA_SPLIT_GREEN_PRESET_MODES = ["Eco", "Disparo"]


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
    # Default for STANDARD/ATW/AQUAPURA/KJRH-120L; overridden by the
    # hvac_modes property below for the Aquapura Split Green, a DHW-only
    # heat pump with no real Cool/Fan-only capability.
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.FAN_ONLY]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.PRESET_MODE
    )
    _attr_target_temperature_step = 1.0
    _attr_preset_modes = AQUAPURA_SPLIT_GREEN_PRESET_MODES

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
    def hvac_modes(self) -> list[HVACMode]:
        # The Aquapura Split Green is a DHW-only heat pump: only power
        # Off/Heat is confirmed (STATUS body[13]), so Cool/Fan-only must not
        # be offered as choices the device can't actually do.
        if self._profile is ModelProfile.AQUAPURA_SPLIT_GREEN:
            return [HVACMode.OFF, HVACMode.HEAT]
        return self._attr_hvac_modes

    @property
    def current_temperature(self) -> float | None:
        if self._sensors is None:
            return None
        # Profile-aware: ATW/AQUAPURA/AQUAPURA_SPLIT_GREEN have no real water-inlet
        # reading, so the meaningful "current" value is the DHW tank temp the
        # profiles surface on th_temp. STANDARD is unchanged: it reads the real
        # inlet (twin_temp).
        if self._profile in (
            ModelProfile.ATW,
            ModelProfile.AQUAPURA,
            ModelProfile.AQUAPURA_SPLIT_GREEN,
        ):
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
    def preset_mode(self) -> str | None:
        # Only the Aquapura Split Green populates operating_mode; every other
        # profile's status leaves it None, so this reads "no preset" for them.
        # An unrecognised marker (reported as "Unknown(0xNN)") also reads as
        # None rather than being forced into one of the two known presets.
        if self._status is None:
            return None
        if self._status.operating_mode not in AQUAPURA_SPLIT_GREEN_PRESET_MODES:
            return None
        return self._status.operating_mode

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self.coordinator.async_set_device(operating_mode=preset_mode)

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
        # KJRH-120L and Aquapura Split Green are DHW heat-pump water heaters;
        # their setpoint range sits above the air-side HEAT range (issue #35).
        # No app-confirmed limit is captured for the Split Green yet, so it
        # reuses the KJRH-120L's validated DHW range as a conservative bound.
        if self._profile is ModelProfile.KJRH120L:
            return float(KJRH120L_TEMP_MIN)
        if self._profile is ModelProfile.AQUAPURA_SPLIT_GREEN:
            return float(AQUAPURA_SPLIT_GREEN_TEMP_MIN)
        set_mode = _HVAC_TO_SET_MODE.get(self.hvac_mode)
        if set_mode is not None and set_mode in TEMP_RANGES:
            return float(TEMP_RANGES[set_mode][0])
        return 10.0

    @property
    def max_temp(self) -> float:
        if self._profile is ModelProfile.KJRH120L:
            return float(KJRH120L_TEMP_MAX)
        if self._profile is ModelProfile.AQUAPURA_SPLIT_GREEN:
            return float(AQUAPURA_SPLIT_GREEN_TEMP_MAX)
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
            # this keeps the short DHW-setpoint write inside the valid range;
            # for other profiles the value is already mode-constrained by HA.
            clamped = max(self.min_temp, min(float(temp), self.max_temp))
            await self.coordinator.async_set_device(temperature=int(clamped))

    async def async_turn_on(self) -> None:
        await self.coordinator.async_set_device(power_on=True)

    async def async_turn_off(self) -> None:
        await self.coordinator.async_set_device(mode=MODE_OFF)
