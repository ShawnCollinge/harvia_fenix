from __future__ import annotations

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
from ._helpers import latest_data, nudge_refresh

import logging
_LOGGER = logging.getLogger(__name__)

# Sauna setpoint bounds. The upper bound is taken from the device's
# settings.maxTemp when available; this is the fallback.
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
    states = (device_coordinator.data or {}).get("states", {})
    devices: list[HarviaDevice] = (device_coordinator.data or {}).get("devices", [])

    entities: list[ClimateEntity] = []
    for dev in devices:
        # Fail open: if the capability flag is missing, still create an enabled climate.
        supported = bool((states.get(dev.id) or {}).get("has_heater", True))
        entities.append(HarviaClimate(hass, entry.entry_id, device_coordinator, dev, supported))

    async_add_entities(entities)


class HarviaClimate(CoordinatorEntity[HarviaDeviceCoordinator], ClimateEntity):
    """Sauna heater as a thermostat.

    Current temperature comes from the fast data coordinator (latest-data
    'temp'); the target temperature and on/off status come from the device
    coordinator. Setting the temperature and toggling heat/off both go through
    the SAUNA command (POST /devices/command).
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

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        coordinator: HarviaDeviceCoordinator,
        device: HarviaDevice,
        supported: bool,
    ) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._entry_id = entry_id
        self._device = device

        self._attr_unique_id = f"{device.id}_climate"
        self._attr_name = f"Harvia {device.type} Sauna"
        self._attr_device_info = build_device_info(device)
        self._attr_entity_registry_enabled_default = supported

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
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        # heater_state reflects actual element activity when reported.
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

        # The setpoint lives on the active profile and is written via the
        # AppSync shadow mutation (the SAUNA REST command is on/off only).
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
                "Harvia CLIMATE set-temperature error device=%s temp=%s profile=%s",
                self._device.id,
                temp,
                active_profile,
            )
            raise

        await self._async_nudge_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._async_send({"state": hvac_mode == HVACMode.HEAT})

    async def async_turn_on(self) -> None:
        await self._async_send({"state": True})

    async def async_turn_off(self) -> None:
        await self._async_send({"state": False})

    async def _async_send(self, command_data: dict[str, Any]) -> None:
        """Send a SAUNA on/off command and nudge both coordinators."""
        payload = {"cabin_id": "C1", **command_data}
        try:
            await self.coordinator.api.async_send_device_command(
                device_id=self._device.id,
                command="SAUNA",
                payload=payload,
            )
        except Exception:
            _LOGGER.exception(
                "Harvia CLIMATE command error device=%s payload=%s",
                self._device.id,
                payload,
            )
            raise

        await self._async_nudge_refresh()

    async def _async_nudge_refresh(self) -> None:
        """Cloud + polling: nudge both coordinators so the UI catches up quickly."""
        await nudge_refresh(self.coordinator, self._data_coordinator)
