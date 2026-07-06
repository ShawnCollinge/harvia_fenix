from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .constants import DOMAIN, DEVICE_COORDINATOR, DATA_COORDINATOR
from .coordinator import HarviaDeviceCoordinator, HarviaDataCoordinator
from .api import HarviaDevice
from .device_info import build_device_info
from ._helpers import latest_data

_LOGGER = logging.getLogger(__name__)

# Sauna setpoint bounds; upper bound falls back to the device's settings.maxTemp.
DEFAULT_MIN_TEMP = 40
DEFAULT_MAX_TEMP = 110
TARGET_TEMP_STEP = 1


def _coerce_float(val: Any) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device_coordinator: HarviaDeviceCoordinator = hass.data[DOMAIN][entry.entry_id][DEVICE_COORDINATOR]
    devices: list[HarviaDevice] = (device_coordinator.data or {}).get("devices", [])
    async_add_entities(HarviaClimate(hass, entry.entry_id, dev) for dev in devices)


class HarviaClimate(CoordinatorEntity[HarviaDeviceCoordinator], ClimateEntity):
    """Sauna as a thermostat: HEAT turns the sauna on, OFF turns it off.

    Target temperature and on/off come from the device coordinator (fed by the
    websocket); current temperature comes from the data coordinator (latest-data
    'temp'). Heat on/off is the SAUNA command; the setpoint is a shadow write.
    """

    _attr_icon = "mdi:sauna"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_target_temperature_step = TARGET_TEMP_STEP
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, hass: HomeAssistant, entry_id: str, device: HarviaDevice) -> None:
        super().__init__(hass.data[DOMAIN][entry_id][DEVICE_COORDINATOR])
        self._hass = hass
        self._entry_id = entry_id
        self._device = device
        self._attr_unique_id = f"{device.id}_climate"
        self._attr_name = f"Harvia {device.type} Sauna"
        self._attr_device_info = build_device_info(device)

    @property
    def _data_coordinator(self) -> HarviaDataCoordinator:
        return self._hass.data[DOMAIN][self._entry_id][DATA_COORDINATOR]

    @property
    def _state(self) -> dict[str, Any]:
        state = (self.coordinator.data or {}).get("states", {}).get(self._device.id)
        return state if isinstance(state, dict) else {}

    async def async_added_to_hass(self) -> None:
        """Also follow the data coordinator for live current-temperature updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._data_coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def current_temperature(self) -> Optional[float]:
        data = latest_data(self._data_coordinator, self._device.id)
        if not isinstance(data, dict):
            return None
        return _coerce_float(data.get("temp"))

    @property
    def target_temperature(self) -> Optional[float]:
        return _coerce_float(self._state.get("target_temperature"))

    @property
    def min_temp(self) -> float:
        return DEFAULT_MIN_TEMP

    @property
    def max_temp(self) -> float:
        max_temp = _coerce_float(self._state.get("setting_max_temp"))
        return max_temp if max_temp is not None else DEFAULT_MAX_TEMP

    @property
    def hvac_mode(self) -> Optional[HVACMode]:
        # Harvia sauna_status: 1 = ON; 0/2/3 = OFF.
        try:
            iv = int(self._state.get("sauna_status"))
        except (TypeError, ValueError):
            return None
        return HVACMode.HEAT if iv == 1 else HVACMode.OFF

    @property
    def hvac_action(self) -> Optional[HVACAction]:
        if self.hvac_mode is None:
            return None
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        heater_state = self._state.get("heater_state")
        if isinstance(heater_state, str):
            s = heater_state.strip().lower()
            if s in ("heating", "on", "active", "running"):
                return HVACAction.HEATING
            if s in ("idle", "ready", "off", "standby"):
                return HVACAction.IDLE
        return HVACAction.HEATING

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        active_profile = self._state.get("active_profile")
        if active_profile is None:
            _LOGGER.error(
                "Harvia CLIMATE cannot set temperature: no active profile for device=%s",
                self._device.id,
            )
            return
        try:
            await self.coordinator.api.async_set_target_temp(
                device_id=self._device.id,
                temp=int(round(temp)),
                active_profile=active_profile,
            )
        except Exception:
            _LOGGER.exception(
                "Harvia CLIMATE set-temperature error device=%s temp=%s",
                self._device.id, temp,
            )
            raise
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._async_set_heat(hvac_mode == HVACMode.HEAT)

    async def async_turn_on(self) -> None:
        await self._async_set_heat(True)

    async def async_turn_off(self) -> None:
        await self._async_set_heat(False)

    async def _async_set_heat(self, on: bool) -> None:
        try:
            await self.coordinator.api.async_send_command(self._device.id, "SAUNA", on)
        except Exception:
            _LOGGER.exception("Harvia CLIMATE SAUNA command error device=%s", self._device.id)
            raise
        await self.coordinator.async_request_refresh()
